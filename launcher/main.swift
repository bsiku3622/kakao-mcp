import Foundation

// Kakao MCP launcher.
//
// macOS grants Accessibility and Screen Recording to the app that is responsible
// for a process, and child processes inherit that app. Running the kakao-mcp
// daemon as a child of this tiny app gives the user one named entry to allow
// ("Kakao MCP") instead of allowing every script that Python runs.
//
// launchd keeps this app alive; it starts `bin/kkt-mcp --http`, forwards
// SIGTERM/SIGINT to it, and exits with the child's status so launchd restarts both.

let root = Bundle.main.object(forInfoDictionaryKey: "KKTRoot") as? String ?? ""
if root.isEmpty {
    FileHandle.standardError.write("Kakao MCP: KKTRoot is missing from Info.plist\n".data(using: .utf8)!)
    exit(78)
}

let fm = FileManager.default
try? fm.createDirectory(atPath: root + "/data", withIntermediateDirectories: true)
let logPath = root + "/data/daemon.log"
if !fm.fileExists(atPath: logPath) { fm.createFile(atPath: logPath, contents: nil) }

let child = Process()
child.executableURL = URL(fileURLWithPath: root + "/bin/kkt-mcp")
child.arguments = ["--http"]
if let log = FileHandle(forWritingAtPath: logPath) {
    log.seekToEndOfFile()
    child.standardOutput = log
    child.standardError = log
}

var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { child.terminate() }
    source.resume()
    signalSources.append(source)
}

child.terminationHandler = { process in exit(process.terminationStatus) }
do {
    try child.run()
} catch {
    FileHandle.standardError.write("Kakao MCP: could not start \(root)/bin/kkt-mcp: \(error)\n".data(using: .utf8)!)
    exit(1)
}
dispatchMain()
