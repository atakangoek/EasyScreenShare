import ReplayKit
import SwiftUI

/// Opens the system "Screen Broadcast" sheet with our extension preselected.
/// The system picker's own button is tiny, so it lives invisibly in the view
/// hierarchy and our big SwiftUI button taps it programmatically.
final class BroadcastPickerController {
    fileprivate weak var picker: RPSystemBroadcastPickerView?

    func present() {
        let button = picker?.subviews.compactMap { $0 as? UIButton }.first
        button?.sendActions(for: .touchUpInside)
    }
}

struct BroadcastPickerView: UIViewRepresentable {
    let controller: BroadcastPickerController

    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        let picker = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 44, height: 44))
        picker.preferredExtension = Bundle.main.object(forInfoDictionaryKey: "BroadcastExtensionBundleID") as? String
        picker.showsMicrophoneButton = false
        controller.picker = picker
        return picker
    }

    func updateUIView(_ uiView: RPSystemBroadcastPickerView, context: Context) {}
}
