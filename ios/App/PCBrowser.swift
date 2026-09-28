import Foundation

/// Looks for EasyScreenShare PCs so the app can show whether one is reachable.
/// Searching here also triggers the Local Network permission prompt, which
/// the broadcast extension then shares. Everything runs on the main queue.
final class PCBrowser: ObservableObject {
    enum Status: Equatable {
        case searching
        case found([String])
        case notFound
        case permissionDenied
    }

    @Published private(set) var status: Status = .searching
    private var finder: PCFinder?
    private var names: [String] = []

    func search() {
        finder?.stop()
        names = []
        status = .searching

        let finder = PCFinder(queue: .main)
        finder.onFound = { [weak self] pc in
            pc.connection.cancel()  // we only wanted to know it's there
            guard let self else { return }
            self.names = (self.names + [pc.name]).sorted()
            self.status = .found(self.names)
        }
        finder.onScanFinished = { [weak self] localNetworkDenied in
            guard let self, self.names.isEmpty else { return }
            self.status = localNetworkDenied ? .permissionDenied : .notFound
        }
        self.finder = finder
        finder.start()
    }
}
