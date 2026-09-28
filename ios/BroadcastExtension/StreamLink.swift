import Foundation
import Network

/// Finds the EasyScreenShare PC via Bonjour and streams messages to it over TCP.
/// All mutable state is confined to `queue`.
final class StreamLink {
    enum LinkError: LocalizedError {
        case noReceiverFound
        case localNetworkDenied
        case disconnected(String)

        var errorDescription: String? {
            switch self {
            case .noReceiverFound:
                return "No EasyScreenShare PC found. Make sure the PC app is running and both devices are on the same Wi-Fi."
            case .localNetworkDenied:
                return "EasyScreenShare needs Local Network access. Turn it on in Settings › Privacy & Security › Local Network."
            case .disconnected(let reason):
                return "The PC stopped the stream (\(reason))."
            }
        }
    }

    /// Called at most once, on an internal queue.
    var onFailure: ((Error) -> Void)?
    var onKeyframeRequest: (() -> Void)?

    /// When more than this is waiting to be sent (slow Wi-Fi), video frames are
    /// dropped until the backlog clears and the next keyframe arrives.
    private static let maxPendingBytes = 1_500_000
    private static let discoveryTimeout: TimeInterval = 15

    private let queue = DispatchQueue(label: "EasyScreenShare.link", qos: .userInteractive)
    private let deviceName: String
    private var browser: NWBrowser?
    private var connection: NWConnection?
    private var candidates: [NWEndpoint] = []
    private var isReady = false
    private var isStopped = false
    private var pendingBytes = 0
    private var needsKeyframe = true

    init(deviceName: String) {
        self.deviceName = deviceName
    }

    func start() {
        queue.async { [self] in
            let browser = NWBrowser(for: .bonjour(type: StreamProtocol.serviceType, domain: nil), using: .tcp)
            browser.browseResultsChangedHandler = { [weak self] results, _ in
                guard let self else { return }
                // Sorted so the phone and the app agree on which PC is used if there are several.
                self.candidates = results.map(\.endpoint).sorted { $0.debugDescription < $1.debugDescription }
                self.connectIfIdle()
            }
            browser.stateUpdateHandler = { [weak self] state in
                switch state {
                case .waiting(let error), .failed(let error):
                    if case .dns(let code) = error, code == StreamProtocol.dnsPolicyDenied {
                        self?.fail(LinkError.localNetworkDenied)
                    } else if case .failed = state {
                        self?.fail(error)
                    }
                default:
                    break
                }
            }
            self.browser = browser
            browser.start(queue: queue)

            queue.asyncAfter(deadline: .now() + Self.discoveryTimeout) { [weak self] in
                guard let self, !self.isReady else { return }
                self.fail(LinkError.noReceiverFound)
            }
        }
    }

    func stop() {
        queue.async { [self] in shutdown() }
    }

    func sendVideo(_ accessUnit: Data, isKeyframe: Bool, ptsMicros: UInt64, orientation: UInt8) {
        queue.async { [self] in
            guard isReady else { return }
            if pendingBytes > Self.maxPendingBytes {
                needsKeyframe = true
                return
            }
            if needsKeyframe && !isKeyframe {
                onKeyframeRequest?()
                return
            }
            needsKeyframe = false

            var payload = Data(capacity: 10 + accessUnit.count)
            payload.append(orientation)
            payload.append(isKeyframe ? 1 : 0)
            payload.appendBigEndian(ptsMicros)
            payload.append(accessUnit)
            send(.video, payload)
        }
    }

    func sendAudio(_ pcm: Data, sampleRate: UInt32, channels: UInt8, ptsMicros: UInt64) {
        queue.async { [self] in
            guard isReady, pendingBytes < Self.maxPendingBytes * 2 else { return }
            var payload = Data(capacity: 13 + pcm.count)
            payload.appendBigEndian(sampleRate)
            payload.append(channels)
            payload.appendBigEndian(ptsMicros)
            payload.append(pcm)
            send(.audio, payload)
        }
    }

    // MARK: - Private (on `queue`)

    private func connectIfIdle() {
        guard connection == nil, !isStopped, let endpoint = candidates.first else { return }

        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let connection = NWConnection(to: endpoint, using: NWParameters(tls: nil, tcp: tcp))
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self, let connection, connection === self.connection else { return }
            switch state {
            case .ready:
                self.didConnect()
            case .waiting(let error), .failed(let error):
                if self.isReady {
                    self.fail(LinkError.disconnected(error.localizedDescription))
                } else {
                    // Couldn't reach this PC; try the next one (or this one again) shortly.
                    connection.cancel()
                    self.connection = nil
                    if !self.candidates.isEmpty {
                        self.candidates.append(self.candidates.removeFirst())
                    }
                    self.queue.asyncAfter(deadline: .now() + 1) { [weak self] in self?.connectIfIdle() }
                }
            default:
                break
            }
        }
        self.connection = connection
        connection.start(queue: queue)
    }

    private func didConnect() {
        isReady = true
        browser?.cancel()
        browser = nil
        let hello: [String: Any] = ["name": deviceName, "version": StreamProtocol.version]
        send(.hello, (try? JSONSerialization.data(withJSONObject: hello)) ?? Data())
        onKeyframeRequest?()
        receiveNextMessage()
    }

    private func receiveNextMessage() {
        connection?.receive(minimumIncompleteLength: 5, maximumLength: 5) { [weak self] header, _, isComplete, error in
            guard let self, !self.isStopped else { return }
            guard error == nil, let header, header.count == 5 else {
                if error != nil || isComplete {
                    self.fail(LinkError.disconnected(error?.localizedDescription ?? "PC window closed"))
                }
                return
            }
            let type = header[header.startIndex]
            let length = header.dropFirst().reduce(0) { ($0 << 8) | Int($1) }
            let handle = { [weak self] in
                if type == StreamProtocol.MessageType.requestKeyframe.rawValue {
                    self?.onKeyframeRequest?()
                }
                self?.receiveNextMessage()
            }
            if length > 0 {
                self.connection?.receive(minimumIncompleteLength: length, maximumLength: length) { _, _, _, _ in handle() }
            } else {
                handle()
            }
        }
    }

    private func send(_ type: StreamProtocol.MessageType, _ payload: Data) {
        guard let connection else { return }
        let message = StreamProtocol.message(type, payload)
        pendingBytes += message.count
        connection.send(content: message, completion: .contentProcessed { [weak self] error in
            guard let self else { return }
            self.pendingBytes -= message.count
            if let error, self.isReady {
                self.fail(LinkError.disconnected(error.localizedDescription))
            }
        })
    }

    private func fail(_ error: Error) {
        guard !isStopped else { return }
        shutdown()
        onFailure?(error)
    }

    private func shutdown() {
        isStopped = true
        isReady = false
        browser?.cancel()
        browser = nil
        connection?.cancel()
        connection = nil
    }
}
