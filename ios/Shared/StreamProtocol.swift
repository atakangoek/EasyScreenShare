import Foundation
import Network

/// Wire protocol shared with the PC receiver (pc/easyscreenshare.py).
/// Every message is `[type: UInt8][payload length: UInt32 big-endian][payload]`.
enum StreamProtocol {
    static let serviceType = "_easyscreenshare._tcp"
    static let version = 1
    /// The PC's default port, probed on every address of the Wi-Fi subnet.
    static let port: NWEndpoint.Port = 50505
    /// The PC opens every connection with a hello `{"service": greetingService, "name": <PC name>}`.
    static let greetingService = "easyscreenshare"

    /// `kDNSServiceErr_PolicyDenied`: the user turned off Local Network access.
    static let dnsPolicyDenied: Int32 = -65570

    enum MessageType: UInt8 {
        /// UTF-8 JSON. PC -> phone first (the greeting), then phone -> PC `{"name": String, "version": Int}`
        case hello = 0x01
        /// `[orientation: UInt8][flags: UInt8, bit0 = keyframe][pts µs: UInt64]` + H.264 Annex B access unit
        case video = 0x02
        /// `[sample rate: UInt32][channels: UInt8][pts µs: UInt64]` + Int16 little-endian interleaved PCM
        case audio = 0x03
        /// PC -> phone, empty payload
        case requestKeyframe = 0x10
    }

    static func message(_ type: MessageType, _ payload: Data) -> Data {
        var data = Data(capacity: 5 + payload.count)
        data.append(type.rawValue)
        data.appendBigEndian(UInt32(payload.count))
        data.append(payload)
        return data
    }
}

extension Data {
    mutating func appendBigEndian<T: FixedWidthInteger>(_ value: T) {
        Swift.withUnsafeBytes(of: value.bigEndian) { append(contentsOf: $0) }
    }
}
