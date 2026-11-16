import Cocoa
import WebKit

// usage: swift snapshot.swift <input.html> <output.png> [width] [height]
final class Delegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var webView: WKWebView!
    var window: NSWindow!
    var out: String!
    var snapSize: NSSize!

    func applicationDidFinishLaunching(_ n: Notification) {
        let args = CommandLine.arguments
        let input = args[1]
        out = args[2]
        let width = CGFloat(args.count > 3 ? (Double(args[3]) ?? 1440) : 1440)
        let height = CGFloat(args.count > 4 ? (Double(args[4]) ?? 2600) : 2600)
        snapSize = NSSize(width: width, height: height)
        let rect = NSRect(x: 0, y: 0, width: width, height: height)
        webView = WKWebView(frame: rect, configuration: WKWebViewConfiguration())
        webView.navigationDelegate = self
        window = NSWindow(contentRect: rect, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = webView
        window.setFrameOrigin(NSPoint(x: -10000, y: -10000))
        let url = URL(fileURLWithPath: input)
        webView.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            let cfg = WKSnapshotConfiguration()
            cfg.rect = NSRect(x: 0, y: 0, width: self.snapSize.width, height: self.snapSize.height)
            cfg.snapshotWidth = NSNumber(value: Double(self.snapSize.width))
            webView.takeSnapshot(with: cfg) { image, error in
                guard let image = image,
                      let tiff = image.tiffRepresentation,
                      let rep = NSBitmapImageRep(data: tiff),
                      let png = rep.representation(using: .png, properties: [:]) else {
                    print("ERR snapshot: \(String(describing: error))")
                    NSApp.terminate(nil)
                    return
                }
                do {
                    try png.write(to: URL(fileURLWithPath: self.out))
                    print("OK \(self.out) \(Int(self.snapSize.width))x\(Int(self.snapSize.height))")
                } catch {
                    print("ERR write: \(error)")
                }
                NSApp.terminate(nil)
            }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        print("ERR nav: \(error)")
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.prohibited)
app.run()
