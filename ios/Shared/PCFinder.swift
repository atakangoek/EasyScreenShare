import Foundation
import Network

/// Finds EasyScreenShare PCs on the local network in two ways at once:
/// Bonjour, and probing the receiver's port on every address of the phone's
/// Wi-Fi subnet. The probing still works where Bonjour is blocked, whether by
/// the router or by iOS (NoAuth for some sideloaded builds). Every candidate
/// is verified by the greeting the PC sends and handed over already connected.
///
/// Not thread-safe: create it, call `start()`/`stop()`, and receive callbacks on `queue`.
final class PCFinder {
    struct PC {
        let name: String
        /// Ready, greeting consumed. The receiver of `onFound` owns it (use it or cancel it).
        let connection: NWConnection
    }

    var onFound: ((PC) -> Void)?
    /// Called once the subnet probe is done. Bonjour keeps looking until `stop()`.
    var onScanFinished: ((_ localNetworkDenied: Bool) -> Void)?

    private static let maxConcurrentProbes = 96
    private static let probeTimeout: TimeInterval = 2.5

    private let queue: DispatchQueue
    private var browser: NWBrowser?
    private var probes: [ObjectIdentifier: NWConnection] = [:]
    private var pendingHosts: [NWEndpoint] = []
    private var foundNames: Set<String> = []
    private var localNetworkDenied = false
    private var scanFinished = false
    private var isStopped = false

    init(queue: DispatchQueue) {
        self.queue = queue
    }

    func start() {
        startBrowser()
        pendingHosts = Self.subnetHosts().map { .hostPort(host: NWEndpoint.Host($0), port: StreamProtocol.port) }
        pumpProbes()
    }

    func stop() {
        isStopped = true
        browser?.cancel()
        browser = nil
        pendingHosts.removeAll()
        for connection in probes.values {
            connection.stateUpdateHandler = nil
            connection.cancel()
        }
        probes.removeAll()
    }

    // MARK: - Bonjour

    private func startBrowser() {
        let browser = NWBrowser(for: .bonjour(type: StreamProtocol.serviceType, domain: nil), using: .tcp)
        browser.browseResultsChangedHandler = { [weak self] _, changes in
            for change in changes {
                if case .added(let result) = change {
                    self?.probe(result.endpoint)
                }
            }
        }
        browser.stateUpdateHandler = { [weak self] state in
            // Bonjour is only a shortcut: failures such as NoAuth are ignored because
            // the subnet probe covers them. A policy denial means no local network at all.
            if case .waiting(let error) = state, case .dns(let code) = error, code == StreamProtocol.dnsPolicyDenied {
                self?.localNetworkDenied = true
            }
        }
        browser.start(queue: queue)
        self.browser = browser
    }

    // MARK: - Probing

    private func pumpProbes() {
        while !isStopped, probes.count < Self.maxConcurrentProbes, !pendingHosts.isEmpty {
            probe(pendingHosts.removeFirst())
        }
        if !isStopped, !scanFinished, probes.isEmpty, pendingHosts.isEmpty {
            scanFinished = true
            onScanFinished?(localNetworkDenied)
        }
    }

    private func probe(_ endpoint: NWEndpoint) {
        guard !isStopped else { return }
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        tcp.connectionTimeout = Int(Self.probeTimeout.rounded(.up))
        let parameters = NWParameters(tls: nil, tcp: tcp)
        parameters.prohibitedInterfaceTypes = [.cellular]

        let connection = NWConnection(to: endpoint, using: parameters)
        probes[ObjectIdentifier(connection)] = connection
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self, let connection else { return }
            switch state {
            case .ready:
                self.readGreeting(connection)
            case .waiting(let error), .failed(let error):
                if connection.currentPath?.unsatisfiedReason == .localNetworkDenied {
                    self.localNetworkDenied = true
                }
                if case .dns(let code) = error, code == StreamProtocol.dnsPolicyDenied {
                    self.localNetworkDenied = true
                }
                self.endProbe(connection, foundName: nil)
            default:
                break
            }
        }
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + Self.probeTimeout) { [weak self, weak connection] in
            guard let self, let connection else { return }
            self.endProbe(connection, foundName: nil)
        }
    }

    private func readGreeting(_ connection: NWConnection) {
        connection.receive(minimumIncompleteLength: 5, maximumLength: 5) { [weak self] header, _, _, error in
            guard let self else { return }
            guard error == nil, let header, header.count == 5,
                  header[header.startIndex] == StreamProtocol.MessageType.hello.rawValue else {
                self.endProbe(connection, foundName: nil)
                return
            }
            let length = header.dropFirst().reduce(0) { ($0 << 8) | Int($1) }
            guard length > 0, length <= 4096 else {
                self.endProbe(connection, foundName: nil)
                return
            }
            connection.receive(minimumIncompleteLength: length, maximumLength: length) { [weak self] body, _, _, error in
                let info = body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                guard error == nil, info?["service"] as? String == StreamProtocol.greetingService else {
                    self?.endProbe(connection, foundName: nil)
                    return
                }
                self?.endProbe(connection, foundName: info?["name"] as? String ?? "PC")
            }
        }
    }

    private func endProbe(_ connection: NWConnection, foundName: String?) {
        guard probes.removeValue(forKey: ObjectIdentifier(connection)) != nil else { return }
        connection.stateUpdateHandler = nil
        if let foundName, !isStopped, foundNames.insert(foundName).inserted {
            onFound?(PC(name: foundName, connection: connection))
        } else {
            connection.cancel()
        }
        pumpProbes()
    }

    // MARK: - Subnet

    /// Every other IPv4 address on the phone's Wi-Fi (or hotspot) networks, at most a /24 each.
    static func subnetHosts() -> [String] {
        var hosts: [String] = []
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0, let first = list else { return [] }
        defer { freeifaddrs(list) }

        for pointer in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let interface = pointer.pointee
            let name = String(cString: interface.ifa_name)
            guard name.hasPrefix("en") || name.hasPrefix("bridge"),
                  let address = interface.ifa_addr, address.pointee.sa_family == UInt8(AF_INET),
                  let netmask = interface.ifa_netmask else { continue }

            let ip = address.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                UInt32(bigEndian: $0.pointee.sin_addr.s_addr)
            }
            var mask = netmask.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                UInt32(bigEndian: $0.pointee.sin_addr.s_addr)
            }
            guard ip >> 16 != 0xA9FE else { continue }  // 169.254.x.x: no real network
            mask = max(mask, 0xFFFF_FF00)
            let network = ip & mask
            let broadcast = network | ~mask
            guard broadcast > network + 1 else { continue }

            // Nearest addresses first: DHCP tends to hand out neighbouring addresses.
            let candidates = ((network + 1)..<broadcast).filter { $0 != ip }
                .sorted { $0.distance(to: ip).magnitude < $1.distance(to: ip).magnitude }
            hosts += candidates.map { "\($0 >> 24).\($0 >> 16 & 0xFF).\($0 >> 8 & 0xFF).\($0 & 0xFF)" }
        }
        return hosts
    }
}
