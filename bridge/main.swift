import Foundation

// kakao-mcp bridge: stdio <-> the local kakao-mcp daemon.
//
// The daemon serves stateless HTTP with JSON responses, so relaying is one POST
// per JSON-RPC line and one reply line back. Requests run concurrently (a long
// send_message does not hold up tools/list). Notifications get 202 and no reply.
// It never touches KakaoTalk, so it needs no macOS permissions and runs fine from
// `ssh macbook kakao-mcp --bridge`. Usage: kakao-mcp-bridge <token file>

let tokenPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
guard let rawToken = try? String(contentsOfFile: tokenPath, encoding: .utf8),
      case let token = rawToken.trimmingCharacters(in: .whitespacesAndNewlines), !token.isEmpty else {
    FileHandle.standardError.write("kakao-mcp bridge: no daemon token at \(tokenPath); start the daemon first\n".data(using: .utf8)!)
    exit(1)
}
let port = ProcessInfo.processInfo.environment["KKT_HTTP_PORT"] ?? "8799"

// Plain sockets instead of URLSession: the peer is always 127.0.0.1, and URLSession
// pulled in CFNetwork (14 MB resident against about 5 MB without it).
struct HTTPError: Error { let message: String }

func post(_ body: Data) throws -> (status: Int, contentType: String, body: Data) {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { throw HTTPError(message: "socket() failed") }
    defer { close(fd) }
    var timeout = timeval(tv_sec: 300, tv_usec: 0)      // opening a room may wait 30 s for the user to rest
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = in_port_t(UInt16(port) ?? 8799).bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let connected = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
    }
    guard connected == 0 else { throw HTTPError(message: "connection refused") }
    var request = Data(("POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:\(port)\r\n" +
                        "Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n" +
                        "Authorization: Bearer \(token)\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n").utf8)
    request.append(body)
    try request.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
        var sent = 0
        while sent < raw.count {
            let n = send(fd, raw.baseAddress! + sent, raw.count - sent, 0)
            guard n > 0 else { throw HTTPError(message: "send() failed") }
            sent += n
        }
    }
    var response = Data(), buffer = [UInt8](repeating: 0, count: 65536)
    while true {                                         // Connection: close - read to EOF
        let n = recv(fd, &buffer, buffer.count, 0)
        if n > 0 { response.append(buffer, count: n) } else if n == 0 { break }
        else { throw HTTPError(message: errno == EAGAIN ? "no reply within 300 s" : "recv() failed") }
    }
    guard let split = response.range(of: Data("\r\n\r\n".utf8)) else { throw HTTPError(message: "malformed reply") }
    let head = String(decoding: response[..<split.lowerBound], as: UTF8.self).components(separatedBy: "\r\n")
    var payload = Data(response[split.upperBound...])
    let status = Int(head.first?.split(separator: " ").dropFirst().first ?? "") ?? 0
    var headers: [String: String] = [:]
    for line in head.dropFirst() {
        if let colon = line.firstIndex(of: ":") {
            headers[line[..<colon].lowercased()] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
    }
    if headers["transfer-encoding"]?.lowercased() == "chunked" { payload = dechunk(payload) }
    return (status, headers["content-type"] ?? "", payload)
}

func dechunk(_ data: Data) -> Data {
    var out = Data(), rest = data[...]
    while let lineEnd = rest.range(of: Data("\r\n".utf8)),
          let size = Int(String(decoding: rest[..<lineEnd.lowerBound], as: UTF8.self)
                            .split(separator: ";").first ?? "", radix: 16), size > 0 {
        let start = lineEnd.upperBound
        guard rest.distance(from: start, to: rest.endIndex) >= size else { break }
        let end = rest.index(start, offsetBy: size)
        out.append(rest[start..<end])
        rest = rest[min(rest.index(end, offsetBy: 2, limitedBy: rest.endIndex) ?? rest.endIndex, rest.endIndex)...]
    }
    return out
}

let outLock = NSLock()
func emit(_ data: Data) {
    var line = data
    if line.contains(0x0A),      // keep one message per line
       let obj = try? JSONSerialization.jsonObject(with: data),
       let compact = try? JSONSerialization.data(withJSONObject: obj, options: [.withoutEscapingSlashes]) {
        line = compact
    }
    line.append(0x0A)
    outLock.lock(); FileHandle.standardOutput.write(line); outLock.unlock()
}

func errorReply(id: Any?, _ message: String) {
    guard let id, !(id is NSNull) else { return }   // notifications get no reply
    let obj: [String: Any] = ["jsonrpc": "2.0", "id": id, "error": ["code": -32000, "message": message]]
    if let data = try? JSONSerialization.data(withJSONObject: obj) { emit(data) }
}

/// Reply bodies are JSON; an SSE body (if the daemon ever streams) is unwrapped from its data: lines.
func messages(in data: Data, contentType: String) -> [Data] {
    guard contentType.contains("text/event-stream"), let text = String(data: data, encoding: .utf8) else { return [data] }
    var out: [Data] = [], current = ""
    for line in text.components(separatedBy: "\n") {
        if line.hasPrefix("data:") { current += line.dropFirst(5).trimmingCharacters(in: .whitespaces) }
        else if line.trimmingCharacters(in: .whitespaces).isEmpty, !current.isEmpty { out.append(Data(current.utf8)); current = "" }
    }
    if !current.isEmpty { out.append(Data(current.utf8)) }
    return out
}

// Exit when the client that started us is gone (reparented to launchd).
let parent = getppid()
Thread.detachNewThread {
    while true { sleep(2); if getppid() != parent { exit(0) } }
}

let pending = DispatchGroup()
while let line = readLine(strippingNewline: true) {
    let trimmed = line.trimmingCharacters(in: .whitespaces)
    if trimmed.isEmpty { continue }
    let body = Data(trimmed.utf8)
    let id = (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["id"]

    pending.enter()
    DispatchQueue.global().async {
        defer { pending.leave() }
        do {
            let reply = try post(body)
            guard !reply.body.isEmpty else {        // 202 for notifications
                if reply.status >= 400 { errorReply(id: id, "kakao-mcp daemon answered HTTP \(reply.status)") }
                return
            }
            let bodies = messages(in: reply.body, contentType: reply.contentType)
            let isJSON = bodies.allSatisfy { (try? JSONSerialization.jsonObject(with: $0)) != nil }
            if reply.status >= 400 && !isJSON {
                errorReply(id: id, "kakao-mcp daemon answered HTTP \(reply.status): \(String(decoding: reply.body.prefix(200), as: UTF8.self))")
                return
            }
            bodies.forEach(emit)
        } catch let error as HTTPError {
            errorReply(id: id, "kakao-mcp daemon on 127.0.0.1:\(port): \(error.message)")
        } catch {
            errorReply(id: id, "kakao-mcp bridge: \(error)")
        }
    }
}
pending.wait()
exit(0)
