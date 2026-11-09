#include <cctype>
#include <chrono>
#include <cmath>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <variant>
#include <vector>

struct Json {
    using Array = std::vector<Json>;
    using Object = std::map<std::string, Json>;
    std::variant<std::nullptr_t, bool, double, std::string, Array, Object> value = nullptr;
};

class Parser {
public:
    explicit Parser(const std::string &text) : text(text) {}
    Json parse() { auto result = value(); space(); if (pos != text.size()) fail("trailing data"); return result; }
private:
    const std::string &text;
    size_t pos = 0;
    [[noreturn]] void fail(const char *message) { throw std::runtime_error(message); }
    void space() { while (pos < text.size() && std::isspace(static_cast<unsigned char>(text[pos]))) ++pos; }
    bool take(char c) { space(); if (pos < text.size() && text[pos] == c) { ++pos; return true; } return false; }
    Json value() {
        space(); if (pos >= text.size()) fail("missing value");
        if (text[pos] == '{') return object(); if (text[pos] == '[') return array(); if (text[pos] == '"') return Json{string()};
        if (text.compare(pos, 4, "true") == 0) { pos += 4; return Json{true}; }
        if (text.compare(pos, 5, "false") == 0) { pos += 5; return Json{false}; }
        if (text.compare(pos, 4, "null") == 0) { pos += 4; return Json{nullptr}; }
        size_t end = pos; while (end < text.size() && std::string("-+.eE0123456789").find(text[end]) != std::string::npos) ++end;
        try { auto result = Json{std::stod(text.substr(pos, end - pos))}; pos = end; return result; } catch (...) { fail("invalid number"); }
    }
    std::string string() {
        if (text[pos++] != '"') fail("expected string"); std::string out;
        while (pos < text.size()) {
            unsigned char c = text[pos++]; if (c == '"') return out; if (c < 0x20) fail("control in string");
            if (c != '\\') { out.push_back(static_cast<char>(c)); continue; }
            if (pos >= text.size()) fail("unfinished escape"); char escaped = text[pos++];
            switch (escaped) {
                case '"': out.push_back('"'); break; case '\\': out.push_back('\\'); break; case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break; case 'f': out.push_back('\f'); break; case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break; case 't': out.push_back('\t'); break;
                case 'u': unicode_escape(out); break;
                default: fail("invalid escape");
            }
        }
        fail("unterminated string");
    }
    unsigned hex4() { if (pos + 4 > text.size()) fail("short unicode"); unsigned code = std::stoul(text.substr(pos, 4), nullptr, 16); pos += 4; return code; }
    void unicode_escape(std::string &out) {
        unsigned code = hex4();
        if (code >= 0xd800 && code <= 0xdbff) { if (pos + 2 > text.size() || text.compare(pos, 2, "\\u") != 0) fail("missing low surrogate"); pos += 2; unsigned low = hex4(); if (low < 0xdc00 || low > 0xdfff) fail("invalid low surrogate"); code = 0x10000 + ((code - 0xd800) << 10) + low - 0xdc00; }
        else if (code >= 0xdc00 && code <= 0xdfff) fail("unexpected low surrogate");
        if (code < 0x80) out.push_back(code); else if (code < 0x800) { out.push_back(0xc0 | (code >> 6)); out.push_back(0x80 | (code & 63)); } else if (code < 0x10000) { out.push_back(0xe0 | (code >> 12)); out.push_back(0x80 | ((code >> 6) & 63)); out.push_back(0x80 | (code & 63)); } else { out.push_back(0xf0 | (code >> 18)); out.push_back(0x80 | ((code >> 12) & 63)); out.push_back(0x80 | ((code >> 6) & 63)); out.push_back(0x80 | (code & 63)); }
    }
    Json array() { ++pos; Json::Array out; if (take(']')) return Json{out}; while (true) { out.push_back(value()); if (take(']')) return Json{out}; if (!take(',')) fail("invalid array"); } }
    Json object() { ++pos; Json::Object out; if (take('}')) return Json{out}; while (true) { space(); auto key = string(); if (!take(':')) fail("missing colon"); out[key] = value(); if (take('}')) return Json{out}; if (!take(',')) fail("invalid object"); } }
};

std::string quote(const std::string &input) {
    std::string out = "\""; const char *hex = "0123456789abcdef";
    for (unsigned char c : input) {
        switch (c) {
            case '"': out += "\\\""; break; case '\\': out += "\\\\"; break; case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break; case '\t': out += "\\t"; break;
            default: if (c < 0x20) { out += "\\u00"; out.push_back(hex[c >> 4]); out.push_back(hex[c & 15]); } else out.push_back(c);
        }
    }
    return out + "\"";
}
const Json *get(const Json *value, const std::string &key) { if (!value) return nullptr; auto object = std::get_if<Json::Object>(&value->value); if (!object) return nullptr; auto found = object->find(key); return found == object->end() ? nullptr : &found->second; }
std::string text(const Json *value) { auto result = value ? std::get_if<std::string>(&value->value) : nullptr; return result ? *result : ""; }
double number(const Json *value) { auto result = value ? std::get_if<double>(&value->value) : nullptr; return result ? *result : 0; }

std::mutex output_mutex;
std::mutex cancel_mutex;
std::set<std::string> cancelled;
void reply(const std::string &id, const std::string &result) { std::lock_guard<std::mutex> lock(output_mutex); std::cout << "{\"jsonrpc\":\"2.0\",\"id\":" << quote(id) << ",\"result\":" << result << "}" << std::endl; }
bool is_cancelled(const std::string &name) { std::lock_guard<std::mutex> lock(cancel_mutex); return cancelled.count(name) != 0; }

int main() {
    const std::string tools = "{\"tools\":[{\"name\":\"echo\",\"description\":\"Return the supplied text\",\"parameters\":{\"type\":\"object\",\"properties\":{\"text\":{\"type\":\"string\"}},\"required\":[\"text\"]},\"mode\":\"parallel\"},{\"name\":\"wait\",\"description\":\"Wait\",\"parameters\":{\"type\":\"object\",\"properties\":{\"seconds\":{\"type\":\"number\"}},\"required\":[\"seconds\"]},\"resources\":[\"cpp-wait\"]}]}";
    std::string line;
    while (std::getline(std::cin, line)) {
        Json request;
        try { request = Parser(line).parse(); } catch (...) { continue; }
        auto method = text(get(&request, "method")); auto id = text(get(&request, "id"));
        if (method == "notifications/cancelled") { std::lock_guard<std::mutex> lock(cancel_mutex); cancelled.insert(text(get(get(&request, "params"), "name"))); continue; }
        if (method == "shutdown") { reply(id, "{}"); break; }
        bool shutdown = false;
        std::thread([request = std::move(request), method, id, tools, shutdown]() mutable {
            if (method == "initialize") reply(id, "{\"protocolVersion\":\"nextloop.stdio/v1\",\"serverInfo\":{\"name\":\"cpp-worker\"}}");
            else if (method == "ping" || method == "shutdown") reply(id, "{}");
            else if (method == "tools/list") reply(id, tools);
            else if (method == "tools/call") {
                auto params = get(&request, "params"); auto name = text(get(params, "name")); auto arguments = get(params, "arguments");
                if (name == "echo") { auto value = text(get(arguments, "text")); reply(id, "{\"content\":" + quote(value) + ",\"structured\":{\"echo\":" + quote(value) + "}}"); }
                else if (name == "wait") { int steps = static_cast<int>(std::ceil(number(get(arguments, "seconds")) * 100)); for (int i = 0; i < steps; ++i) { if (is_cancelled("wait")) return; std::this_thread::sleep_for(std::chrono::milliseconds(10)); } reply(id, "{\"content\":\"done\"}"); }
                else { std::lock_guard<std::mutex> lock(output_mutex); std::cout << "{\"jsonrpc\":\"2.0\",\"id\":" << quote(id) << ",\"error\":{\"code\":-32601,\"message\":\"Unknown Tool\"}}" << std::endl; }
            } else { std::lock_guard<std::mutex> lock(output_mutex); std::cout << "{\"jsonrpc\":\"2.0\",\"id\":" << quote(id) << ",\"error\":{\"code\":-32601,\"message\":\"Unknown method\"}}" << std::endl; }
        }).detach();
        if (shutdown) break;
    }
}
