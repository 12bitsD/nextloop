#include <chrono>
#include <iostream>
#include <optional>
#include <string>
#include <thread>

std::optional<std::string> value(const std::string &line, const std::string &key) {
    const std::string needle = "\"" + key + "\":\"";
    const auto start = line.find(needle);
    if (start == std::string::npos) return std::nullopt;
    const auto content = start + needle.size();
    const auto end = line.find('"', content);
    if (end == std::string::npos) return std::nullopt;
    return line.substr(content, end - content);
}

double number(const std::string &line, const std::string &key) {
    const std::string needle = "\"" + key + "\":";
    const auto start = line.find(needle);
    if (start == std::string::npos) return 0;
    return std::stod(line.substr(start + needle.size()));
}

void reply(const std::string &id, const std::string &result) {
    std::cout << "{\"jsonrpc\":\"2.0\",\"id\":\"" << id << "\",\"result\":" << result << "}" << std::endl;
}

int main() {
    std::string line;
    while (std::getline(std::cin, line)) {
        const auto method = value(line, "method").value_or("");
        const auto id = value(line, "id").value_or("");
        if (method == "initialize") {
            reply(id, "{\"protocolVersion\":\"nextloop.stdio/v1\",\"serverInfo\":{\"name\":\"cpp-worker\"}}");
        } else if (method == "ping" || method == "shutdown") {
            reply(id, "{}");
        } else if (method == "tools/list") {
            reply(id, "{\"tools\":[{\"name\":\"echo\",\"description\":\"Return the supplied text\",\"parameters\":{\"type\":\"object\",\"properties\":{\"text\":{\"type\":\"string\"}},\"required\":[\"text\"]},\"mode\":\"parallel\"},{\"name\":\"wait\",\"description\":\"Wait\",\"parameters\":{\"type\":\"object\",\"properties\":{\"seconds\":{\"type\":\"number\"}},\"required\":[\"seconds\"]},\"resources\":[\"cpp-wait\"]}]}");
        } else if (method == "tools/call") {
            const auto name = value(line, "name").value_or("");
            if (name == "echo") {
                const auto text = value(line, "text").value_or("");
                reply(id, "{\"content\":\"" + text + "\",\"structured\":{\"echo\":\"" + text + "\"}}");
            } else if (name == "wait") {
                std::this_thread::sleep_for(std::chrono::milliseconds(static_cast<int>(number(line, "seconds") * 1000)));
                reply(id, "{\"content\":\"done\"}");
            } else {
                std::cout << "{\"jsonrpc\":\"2.0\",\"id\":\"" << id << "\",\"error\":{\"code\":-32601,\"message\":\"Unknown Tool\"}}" << std::endl;
            }
        } else if (method != "notifications/cancelled") {
            std::cout << "{\"jsonrpc\":\"2.0\",\"id\":\"" << id << "\",\"error\":{\"code\":-32601,\"message\":\"Unknown method\"}}" << std::endl;
        }
        if (method == "shutdown") break;
    }
}
