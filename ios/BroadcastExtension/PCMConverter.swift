import CoreMedia

/// Converts ReplayKit's app-audio buffers (whose format varies by device and iOS
/// version: Int16 big- or little-endian, Float32, interleaved or not) into the
/// one format the PC expects: Int16 little-endian, interleaved, 1–2 channels.
enum PCMConverter {
    struct PCM {
        let data: Data
        let sampleRate: UInt32
        let channels: UInt8
    }

    static func int16Interleaved(_ sampleBuffer: CMSampleBuffer) -> PCM? {
        guard let format = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(format)?.pointee,
              asbd.mFormatID == kAudioFormatLinearPCM else { return nil }

        let frames = CMSampleBufferGetNumSamples(sampleBuffer)
        let sourceChannels = Int(asbd.mChannelsPerFrame)
        let bytesPerSample = Int(asbd.mBitsPerChannel) / 8
        let isFloat = asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0
        let isBigEndian = asbd.mFormatFlags & kAudioFormatFlagIsBigEndian != 0
        let isNonInterleaved = asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
        guard frames > 0, sourceChannels > 0, [2, 4].contains(bytesPerSample) else { return nil }

        var listSize = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer, bufferListSizeNeededOut: &listSize, bufferListOut: nil, bufferListSize: 0,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil, flags: 0, blockBufferOut: nil)
        let listMemory = UnsafeMutableRawPointer.allocate(
            byteCount: listSize, alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { listMemory.deallocate() }
        let list = listMemory.bindMemory(to: AudioBufferList.self, capacity: 1)
        var blockBuffer: CMBlockBuffer?
        guard CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer, bufferListSizeNeededOut: nil, bufferListOut: list, bufferListSize: listSize,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment, blockBufferOut: &blockBuffer
        ) == noErr else { return nil }
        let buffers = UnsafeMutableAudioBufferListPointer(list)

        let outChannels = min(sourceChannels, 2)
        var out = [Int16](repeating: 0, count: frames * outChannels)

        for channel in 0..<outChannels {
            let buffer = isNonInterleaved ? buffers[min(channel, buffers.count - 1)] : buffers[0]
            guard let base = buffer.mData else { continue }
            let byteCount = Int(buffer.mDataByteSize)
            let stride = isNonInterleaved ? 1 : sourceChannels
            let first = isNonInterleaved ? 0 : channel

            for frame in 0..<frames {
                let offset = (frame * stride + first) * bytesPerSample
                guard offset + bytesPerSample <= byteCount else { break }
                let sample: Int16
                if bytesPerSample == 2 {
                    let raw = base.loadUnaligned(fromByteOffset: offset, as: UInt16.self)
                    sample = Int16(bitPattern: isBigEndian ? raw.byteSwapped : raw.littleEndian)
                } else {
                    let raw = base.loadUnaligned(fromByteOffset: offset, as: UInt32.self)
                    let bits = isBigEndian ? raw.byteSwapped : raw.littleEndian
                    if isFloat {
                        let value = max(-1, min(1, Float(bitPattern: bits)))
                        sample = Int16(value * Float(Int16.max))
                    } else {
                        sample = Int16(truncatingIfNeeded: Int32(bitPattern: bits) >> 16)
                    }
                }
                out[frame * outChannels + channel] = sample.littleEndian
            }
        }

        let data = out.withUnsafeBufferPointer { Data(buffer: $0) }
        return PCM(data: data, sampleRate: UInt32(asbd.mSampleRate), channels: UInt8(outChannels))
    }
}
