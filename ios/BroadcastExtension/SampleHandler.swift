import ReplayKit
import UIKit

/// Entry point of the Broadcast Upload Extension. iOS starts it when the user
/// picks "EasyScreenShare" in the screen broadcast sheet and feeds it every
/// screen frame and app-audio buffer, which we encode and send to the PC.
@objc(SampleHandler)
final class SampleHandler: RPBroadcastSampleHandler {
    private var link: StreamLink?
    private var encoder: H264Encoder?

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        let link = StreamLink(deviceName: UIDevice.current.name)
        let encoder = H264Encoder { [weak link] accessUnit, isKeyframe, ptsMicros, orientation in
            link?.sendVideo(accessUnit, isKeyframe: isKeyframe, ptsMicros: ptsMicros, orientation: orientation)
        }
        link.onKeyframeRequest = { [weak encoder] in encoder?.requestKeyframe() }
        link.onFailure = { [weak self] error in
            // Stops the broadcast and shows `error` to the user.
            self?.finishBroadcastWithError(error)
        }
        self.link = link
        self.encoder = encoder
        link.start()
    }

    override func broadcastFinished() {
        link?.stop()
        encoder?.invalidate()
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        switch sampleBufferType {
        case .video:
            // Frames always arrive in the phone's portrait layout; this tells the
            // PC how to rotate them when the phone is held sideways.
            let orientation = (CMGetAttachment(sampleBuffer, key: RPVideoSampleOrientationKey as CFString,
                                               attachmentModeOut: nil) as? NSNumber)?.uint8Value ?? 1
            encoder?.encode(sampleBuffer, orientation: orientation)
        case .audioApp:
            guard let pcm = PCMConverter.int16Interleaved(sampleBuffer) else { return }
            let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
            link?.sendAudio(pcm.data, sampleRate: pcm.sampleRate, channels: pcm.channels,
                            ptsMicros: pts.isValid ? UInt64(max(0, pts.seconds) * 1_000_000) : 0)
        case .audioMic:
            break
        @unknown default:
            break
        }
    }
}
