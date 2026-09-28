import CoreMedia
import VideoToolbox

/// Hardware H.264 encoder that outputs Annex B access units, with SPS/PPS in
/// front of every keyframe so the PC can start decoding at any keyframe.
final class H264Encoder {
    typealias Output = (_ accessUnit: Data, _ isKeyframe: Bool, _ ptsMicros: UInt64, _ orientation: UInt8) -> Void

    /// Frames larger than this (on their long side) are downscaled before encoding.
    var maxLongSide = 1920
    var bitrate = 8_000_000
    var frameRate = 60

    private static let startCode: [UInt8] = [0, 0, 0, 1]

    private let output: Output
    private let sessionLock = NSLock()
    private var session: VTCompressionSession?
    private var transfer: VTPixelTransferSession?
    private var width = 0
    private var height = 0

    private let keyframeLock = NSLock()
    private var keyframeRequested = true

    init(output: @escaping Output) {
        self.output = output
    }

    deinit {
        invalidate()
    }

    func requestKeyframe() {
        keyframeLock.lock()
        keyframeRequested = true
        keyframeLock.unlock()
    }

    private func takeKeyframeRequest() -> Bool {
        keyframeLock.lock()
        defer { keyframeLock.unlock() }
        let requested = keyframeRequested
        keyframeRequested = false
        return requested
    }

    func encode(_ sampleBuffer: CMSampleBuffer, orientation: UInt8) {
        guard let source = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        sessionLock.lock()
        defer { sessionLock.unlock() }

        let sourceWidth = CVPixelBufferGetWidth(source)
        let sourceHeight = CVPixelBufferGetHeight(source)
        let scale = min(1.0, Double(maxLongSide) / Double(max(sourceWidth, sourceHeight)))
        let targetWidth = Int(Double(sourceWidth) * scale) & ~1
        let targetHeight = Int(Double(sourceHeight) * scale) & ~1
        if session == nil || targetWidth != width || targetHeight != height {
            makeSession(width: targetWidth, height: targetHeight)
        }
        guard let session else { return }

        var frame = source
        if targetWidth != sourceWidth || targetHeight != sourceHeight {
            guard let scaled = scaledCopy(of: source, for: session) else { return }
            frame = scaled
        }

        let frameProperties: CFDictionary? = takeKeyframeRequest()
            ? [kVTEncodeFrameOptionKey_ForceKeyFrame as String: true] as CFDictionary
            : nil
        VTCompressionSessionEncodeFrame(
            session,
            imageBuffer: frame,
            presentationTimeStamp: CMSampleBufferGetPresentationTimeStamp(sampleBuffer),
            duration: .invalid,
            frameProperties: frameProperties,
            infoFlagsOut: nil
        ) { [weak self] status, _, encoded in
            guard status == noErr, let encoded, let self else { return }
            self.emit(encoded, orientation: orientation)
        }
    }

    func invalidate() {
        sessionLock.lock()
        defer { sessionLock.unlock() }
        invalidateLocked()
    }

    private func invalidateLocked() {
        if let session {
            VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(session)
        }
        session = nil
        if let transfer {
            VTPixelTransferSessionInvalidate(transfer)
        }
        transfer = nil
    }

    private func makeSession(width: Int, height: Int) {
        invalidateLocked()
        self.width = width
        self.height = height

        let imageAttributes: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
        ]
        var newSession: VTCompressionSession?
        let status = VTCompressionSessionCreate(
            allocator: nil,
            width: Int32(width),
            height: Int32(height),
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: nil,
            imageBufferAttributes: imageAttributes as CFDictionary,
            compressedDataAllocator: nil,
            outputCallback: nil,
            refcon: nil,
            compressionSessionOut: &newSession
        )
        guard status == noErr, let newSession else { return }

        let properties: [(CFString, CFTypeRef)] = [
            (kVTCompressionPropertyKey_RealTime, kCFBooleanTrue),
            (kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse),
            (kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_High_AutoLevel),
            (kVTCompressionPropertyKey_AverageBitRate, NSNumber(value: bitrate)),
            (kVTCompressionPropertyKey_DataRateLimits, [bitrate * 2 / 8, 1] as CFArray),
            (kVTCompressionPropertyKey_ExpectedFrameRate, NSNumber(value: frameRate)),
            (kVTCompressionPropertyKey_MaxKeyFrameInterval, NSNumber(value: frameRate * 2)),
            (kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, NSNumber(value: 2)),
        ]
        for (key, value) in properties {
            VTSessionSetProperty(newSession, key: key, value: value)
        }
        VTCompressionSessionPrepareToEncodeFrames(newSession)
        session = newSession
        requestKeyframe()
    }

    private func scaledCopy(of source: CVPixelBuffer, for session: VTCompressionSession) -> CVPixelBuffer? {
        guard let pool = VTCompressionSessionGetPixelBufferPool(session) else { return nil }
        var scaled: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &scaled) == kCVReturnSuccess, let scaled else { return nil }
        if transfer == nil {
            VTPixelTransferSessionCreate(allocator: nil, pixelTransferSessionOut: &transfer)
        }
        guard let transfer, VTPixelTransferSessionTransferImage(transfer, from: source, to: scaled) == noErr else {
            return nil
        }
        return scaled
    }

    /// Converts VideoToolbox's AVCC output (length-prefixed NAL units) to Annex B.
    private func emit(_ sample: CMSampleBuffer, orientation: UInt8) {
        guard let block = CMSampleBufferGetDataBuffer(sample),
              let format = CMSampleBufferGetFormatDescription(sample) else { return }

        let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[CFString: Any]]
        let isKeyframe = !(attachments?.first?[kCMSampleAttachmentKey_NotSync] as? Bool ?? false)

        var parameterSetCount = 0
        var nalHeaderLength: Int32 = 4
        CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
            format, parameterSetIndex: 0, parameterSetPointerOut: nil, parameterSetSizeOut: nil,
            parameterSetCountOut: &parameterSetCount, nalUnitHeaderLengthOut: &nalHeaderLength)

        var accessUnit = Data()
        if isKeyframe {
            for index in 0..<parameterSetCount {
                var pointer: UnsafePointer<UInt8>?
                var length = 0
                guard CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                    format, parameterSetIndex: index, parameterSetPointerOut: &pointer,
                    parameterSetSizeOut: &length, parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil
                ) == noErr, let pointer else { continue }
                accessUnit.append(contentsOf: Self.startCode)
                accessUnit.append(pointer, count: length)
            }
        }

        let total = CMBlockBufferGetDataLength(block)
        var avcc = [UInt8](repeating: 0, count: total)
        guard CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: total, destination: &avcc) == kCMBlockBufferNoErr
        else { return }

        let headerLength = Int(nalHeaderLength)
        var offset = 0
        while offset + headerLength <= total {
            var nalLength = 0
            for i in 0..<headerLength {
                nalLength = (nalLength << 8) | Int(avcc[offset + i])
            }
            offset += headerLength
            guard nalLength > 0, offset + nalLength <= total else { break }
            accessUnit.append(contentsOf: Self.startCode)
            accessUnit.append(contentsOf: avcc[offset..<(offset + nalLength)])
            offset += nalLength
        }

        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        output(accessUnit, isKeyframe, pts.isValid ? UInt64(max(0, pts.seconds) * 1_000_000) : 0, orientation)
    }
}
