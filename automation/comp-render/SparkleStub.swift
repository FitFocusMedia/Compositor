import AppKit

/// Stands in for Sparkle's updater so the app's sources compile into a command-line tool, which never updates itself.
final class SPUStandardUpdaterController: NSObject {
    init(startingUpdater: Bool, updaterDelegate: AnyObject?, userDriverDelegate: AnyObject?) {}
    func startUpdater() {}
    func checkForUpdates(_ sender: Any?) {}
}
