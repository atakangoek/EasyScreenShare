import Foundation
import Network

/// Looks for EasyScreenShare PCs on the local network so the app can show
/// whether one is reachable. Browsing here also triggers the Local Network
/// permission prompt, which the broadcast extension then shares.
@MainActor
final class PCBrowser: ObservableObject {
    enum Status: Equatable {
        case searching
        case found([String])
        case permissionDenied
        case failed(String)
    }

    @Published private(set) var status: Status = .searching
    private var browser: NWBrowser?

    func start() {
        guard browser == nil else { return }
        let browser = NWBrowser(for: .bonjour(type: StreamProtocol.serviceType, domain: nil), using: .tcp)
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            let names = results.map(\.endpoint).sorted { $0.debugDescription < $1.debugDescription }.compactMap { endpoint -> String? in
                if case let .service(name, _, _, _) = endpoint { return name }
                return nil
            }
            Task { @MainActor in self?.status = names.isEmpty ? .searching : .found(names) }
        }
        browser.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in
                switch state {
                case .waiting(let error), .failed(let error):
                    if case .dns(let code) = error, code == StreamProtocol.dnsPolicyDenied {
                        self?.status = .permissionDenied
                    } else if case .failed = state {
                        self?.status = .failed(error.localizedDescription)
                    }
                case .ready:
                    if self?.status == .permissionDenied { self?.status = .searching }
                default:
                    break
                }
            }
        }
        browser.start(queue: .main)
        self.browser = browser
    }

    func restart() {
        browser?.cancel()
        browser = nil
        status = .searching
        start()
    }
}
