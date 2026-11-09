use std::collections::{HashMap, HashSet};
use std::io::{self, BufRead, Write};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

#[derive(Clone)]
enum Json {
    Null,
    Bool(bool),
    Number(f64),
    String(String),
    Array(Vec<Json>),
    Object(HashMap<String, Json>),
}

struct Parser<'a> { bytes: &'a [u8], pos: usize }
impl<'a> Parser<'a> {
    fn new(text: &'a str) -> Self { Self { bytes: text.as_bytes(), pos: 0 } }
    fn parse(mut self) -> Result<Json, String> { let value = self.value()?; self.space(); if self.pos == self.bytes.len() { Ok(value) } else { Err("trailing data".into()) } }
    fn space(&mut self) { while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_whitespace() { self.pos += 1; } }
    fn value(&mut self) -> Result<Json, String> {
        self.space();
        match self.bytes.get(self.pos).copied() {
            Some(b'{') => self.object(), Some(b'[') => self.array(), Some(b'"') => self.string().map(Json::String),
            Some(b't') => { self.literal(b"true")?; Ok(Json::Bool(true)) },
            Some(b'f') => { self.literal(b"false")?; Ok(Json::Bool(false)) },
            Some(b'n') => { self.literal(b"null")?; Ok(Json::Null) },
            Some(b'-' | b'0'..=b'9') => self.number(), _ => Err(format!("invalid value at {}", self.pos)),
        }
    }
    fn literal(&mut self, text: &[u8]) -> Result<(), String> { if self.bytes.get(self.pos..self.pos + text.len()) == Some(text) { self.pos += text.len(); Ok(()) } else { Err("invalid literal".into()) } }
    fn number(&mut self) -> Result<Json, String> { let start = self.pos; while self.pos < self.bytes.len() && matches!(self.bytes[self.pos], b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9') { self.pos += 1; } let text = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|e| e.to_string())?; Ok(Json::Number(text.parse().map_err(|_| "invalid number")?)) }
    fn string(&mut self) -> Result<String, String> {
        self.pos += 1; let mut output = String::new();
        while let Some(byte) = self.bytes.get(self.pos).copied() {
            self.pos += 1;
            match byte {
                b'"' => return Ok(output), b'\\' => { let escaped = self.bytes.get(self.pos).copied().ok_or("unterminated escape")?; self.pos += 1; match escaped {
                    b'"' => output.push('"'), b'\\' => output.push('\\'), b'/' => output.push('/'), b'b' => output.push('\u{8}'), b'f' => output.push('\u{c}'), b'n' => output.push('\n'), b'r' => output.push('\r'), b't' => output.push('\t'),
                    b'u' => output.push(self.unicode_escape()?),
                    _ => return Err("invalid escape".into()),
                }}, b if b < 0x20 => return Err("control byte in string".into()),
                _ => { let start = self.pos - 1; while self.pos < self.bytes.len() && self.bytes[self.pos] >= 0x80 { self.pos += 1; } output.push_str(std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|e| e.to_string())?); }
            }
        }
        Err("unterminated string".into())
    }
    fn unicode_escape(&mut self) -> Result<char, String> {
        fn hex4(bytes: &[u8], pos: &mut usize) -> Result<u32, String> {
            let hex = bytes.get(*pos..*pos + 4).ok_or("short unicode escape")?;
            *pos += 4;
            u32::from_str_radix(std::str::from_utf8(hex).map_err(|e| e.to_string())?, 16).map_err(|_| "invalid unicode escape".into())
        }
        let high = hex4(self.bytes, &mut self.pos)?;
        let value = if (0xd800..=0xdbff).contains(&high) {
            if self.bytes.get(self.pos..self.pos + 2) != Some(b"\\u") { return Err("missing low surrogate".into()); }
            self.pos += 2;
            let low = hex4(self.bytes, &mut self.pos)?;
            if !(0xdc00..=0xdfff).contains(&low) { return Err("invalid low surrogate".into()); }
            0x10000 + ((high - 0xd800) << 10) + low - 0xdc00
        } else if (0xdc00..=0xdfff).contains(&high) { return Err("unexpected low surrogate".into()); } else { high };
        char::from_u32(value).ok_or("invalid unicode scalar".into())
    }
    fn array(&mut self) -> Result<Json, String> { self.pos += 1; let mut items = Vec::new(); loop { self.space(); if self.bytes.get(self.pos) == Some(&b']') { self.pos += 1; return Ok(Json::Array(items)); } items.push(self.value()?); self.space(); match self.bytes.get(self.pos) { Some(b',') => self.pos += 1, Some(b']') => { self.pos += 1; return Ok(Json::Array(items)); }, _ => return Err("invalid array".into()) } } }
    fn object(&mut self) -> Result<Json, String> { self.pos += 1; let mut items = HashMap::new(); loop { self.space(); if self.bytes.get(self.pos) == Some(&b'}') { self.pos += 1; return Ok(Json::Object(items)); } if self.bytes.get(self.pos) != Some(&b'"') { return Err("object key must be string".into()); } let key = self.string()?; self.space(); if self.bytes.get(self.pos) != Some(&b':') { return Err("missing colon".into()); } self.pos += 1; items.insert(key, self.value()?); self.space(); match self.bytes.get(self.pos) { Some(b',') => self.pos += 1, Some(b'}') => { self.pos += 1; return Ok(Json::Object(items)); }, _ => return Err("invalid object".into()) } } }
}

fn string(value: &str) -> String { let mut out = String::from("\""); for ch in value.chars() { match ch { '"' => out.push_str("\\\""), '\\' => out.push_str("\\\\"), '\n' => out.push_str("\\n"), '\r' => out.push_str("\\r"), '\t' => out.push_str("\\t"), c if c < '\u{20}' => out.push_str(&format!("\\u{:04x}", c as u32)), c => out.push(c) } } out.push('"'); out }
fn get<'a>(value: &'a Json, key: &str) -> Option<&'a Json> { match value { Json::Object(map) => map.get(key), _ => None } }
fn text(value: Option<&Json>) -> String { match value { Some(Json::String(text)) => text.clone(), _ => String::new() } }
fn number(value: Option<&Json>) -> f64 { match value { Some(Json::Number(number)) => *number, _ => 0.0 } }
fn reply(id: &str, result: String, output: &Arc<Mutex<io::Stdout>>) { let mut stdout = output.lock().unwrap(); writeln!(stdout, "{{\"jsonrpc\":\"2.0\",\"id\":{},\"result\":{}}}", string(id), result).unwrap(); stdout.flush().unwrap(); }

fn main() {
    let tools = "{\"tools\":[{\"name\":\"echo\",\"description\":\"Return the supplied text\",\"parameters\":{\"type\":\"object\",\"properties\":{\"text\":{\"type\":\"string\"}},\"required\":[\"text\"]},\"mode\":\"parallel\"},{\"name\":\"wait\",\"description\":\"Wait\",\"parameters\":{\"type\":\"object\",\"properties\":{\"seconds\":{\"type\":\"number\"}},\"required\":[\"seconds\"]},\"resources\":[\"rust-wait\"]}]}";
    let output = Arc::new(Mutex::new(io::stdout()));
    let cancelled = Arc::new(Mutex::new(HashSet::<String>::new()));
    for line in io::stdin().lock().lines() {
        let request = match Parser::new(&line.unwrap()).parse() { Ok(Json::Object(map)) => map, _ => continue };
        let method = text(request.get("method"));
        let id = text(request.get("id"));
        if method == "notifications/cancelled" { if let Some(Json::Object(params)) = request.get("params") { cancelled.lock().unwrap().insert(text(params.get("name"))); } continue; }
        if method == "shutdown" { reply(&id, "{}".into(), &output); break; }
        let output = Arc::clone(&output); let cancelled = Arc::clone(&cancelled);
        let shutdown = false;
        thread::spawn(move || {
            match method.as_str() {
                "initialize" => reply(&id, "{\"protocolVersion\":\"nextloop.stdio/v1\",\"serverInfo\":{\"name\":\"rust-worker\"}}".into(), &output),
                "ping" | "shutdown" => reply(&id, "{}".into(), &output),
                "tools/list" => reply(&id, tools.into(), &output),
                "tools/call" => {
                    let params = request.get("params"); let name = text(get(params.unwrap_or(&Json::Null), "name")); let arguments = get(params.unwrap_or(&Json::Null), "arguments").unwrap_or(&Json::Null);
                    if name == "echo" { let value = text(get(arguments, "text")); reply(&id, format!("{{\"content\":{},\"structured\":{{\"echo\":{}}}}}", string(&value), string(&value)), &output); }
                    else if name == "wait" { let duration = number(get(arguments, "seconds")); let steps = (duration * 100.0).ceil() as usize; for _ in 0..steps { if cancelled.lock().unwrap().contains("wait") { return; } thread::sleep(Duration::from_millis(10)); } reply(&id, "{\"content\":\"done\"}".into(), &output); }
                    else { let mut stdout = output.lock().unwrap(); writeln!(stdout, "{{\"jsonrpc\":\"2.0\",\"id\":{},\"error\":{{\"code\":-32601,\"message\":\"Unknown Tool\"}}}}", string(&id)).unwrap(); }
                }
                _ => { let mut stdout = output.lock().unwrap(); writeln!(stdout, "{{\"jsonrpc\":\"2.0\",\"id\":{},\"error\":{{\"code\":-32601,\"message\":\"Unknown method\"}}}}", string(&id)).unwrap(); }
            }
        });
        if shutdown { break; }
    }
}
