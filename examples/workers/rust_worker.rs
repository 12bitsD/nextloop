use std::io::{self, BufRead, Write};
use std::thread;
use std::time::Duration;

fn value<'a>(line: &'a str, key: &str) -> Option<&'a str> {
    let needle = format!("\"{}\":\"", key);
    let start = line.find(&needle)? + needle.len();
    let rest = &line[start..];
    let end = rest.find('"')?;
    Some(&rest[..end])
}

fn number(line: &str, key: &str) -> Option<f64> {
    let needle = format!("\"{}\":", key);
    let start = line.find(&needle)? + needle.len();
    let rest = &line[start..];
    let end = rest.find(|c: char| !c.is_ascii_digit() && c != '.' && c != '-')?;
    rest[..end].parse().ok()
}

fn reply(id: &str, result: &str) {
    println!("{{\"jsonrpc\":\"2.0\",\"id\":\"{}\",\"result\":{}}}", id, result);
    io::stdout().flush().unwrap();
}

fn main() {
    let stdin = io::stdin();
    for line in stdin.lock().lines() {
        let line = line.unwrap();
        let method = value(&line, "method").unwrap_or("");
        let id = value(&line, "id").unwrap_or("");
        match method {
            "initialize" => reply(id, "{\"protocolVersion\":\"nextloop.stdio/v1\",\"serverInfo\":{\"name\":\"rust-worker\"}}"),
            "ping" | "shutdown" => reply(id, "{}"),
            "tools/list" => reply(id, "{\"tools\":[{\"name\":\"echo\",\"description\":\"Return the supplied text\",\"parameters\":{\"type\":\"object\",\"properties\":{\"text\":{\"type\":\"string\"}},\"required\":[\"text\"]},\"mode\":\"parallel\"},{\"name\":\"wait\",\"description\":\"Wait\",\"parameters\":{\"type\":\"object\",\"properties\":{\"seconds\":{\"type\":\"number\"}},\"required\":[\"seconds\"]},\"resources\":[\"rust-wait\"]}]}"),
            "tools/call" => {
                let name = value(&line, "name").unwrap_or("");
                if name == "echo" {
                    let text = value(&line, "text").unwrap_or("");
                    reply(id, &format!("{{\"content\":\"{}\",\"structured\":{{\"echo\":\"{}\"}}}}", text, text));
                } else if name == "wait" {
                    thread::sleep(Duration::from_secs_f64(number(&line, "seconds").unwrap_or(0.0)));
                    reply(id, "{\"content\":\"done\"}");
                } else {
                    println!("{{\"jsonrpc\":\"2.0\",\"id\":\"{}\",\"error\":{{\"code\":-32601,\"message\":\"Unknown Tool\"}}}}", id);
                }
            }
            "notifications/cancelled" => {}
            _ => println!("{{\"jsonrpc\":\"2.0\",\"id\":\"{}\",\"error\":{{\"code\":-32601,\"message\":\"Unknown method\"}}}}", id),
        }
        if method == "shutdown" { break; }
    }
}
