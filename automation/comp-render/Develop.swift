import Foundation
import CoreGraphics

// Lightroom / Camera Raw presets (.xmp) read onto Compositor's Camera Raw filter, for `comp-render preset` and
// `comp-render develop`. Each setting with a counterpart is mapped by name; white balance is converted from Kelvin;
// what has none is reported, never dropped silently. The arithmetic is Compositor's, so a grade comes out close
// to Lightroom's in character rather than identical to it.

nonisolated enum XMPNamespace {
    static let crs = "http://ns.adobe.com/camera-raw-settings/1.0/"
}

/// A preset's settings as written: scalars, lists (tone curves), local corrections and its profile.
struct LightroomPreset {
    var name = ""
    var values: [String: String] = [:]
    var lists: [String: [String]] = [:]
    var corrections: [LocalCorrection] = []
    var look: String?
    var lensBlurActive = false

    struct LocalCorrection: Encodable {
        var name: String
        var active: Bool
        var amount: Double
        /// The correction's own sliders, e.g. LocalExposure2012, by their XMP names.
        var settings: [String: Double]
        /// Each mask's XMP attributes (What, Top, Left, Bottom, Right, Feather, Flipped, MaskInverted, ZeroX…).
        var masks: [[String: String]]
    }

    init(url: URL) throws {
        let document = try XMLDocument(contentsOf: url, options: [])
        let descriptions = try document.nodes(forXPath: "//*[local-name()='RDF']/*[local-name()='Description']")
        guard let top = descriptions.compactMap({ $0 as? XMLElement }).first(where: { Self.crsAttributes($0).isEmpty == false })
        else { throw CocoaError(.fileReadCorruptFile) }
        values = Self.crsAttributes(top)
        for child in top.children ?? [] {
            guard let element = child as? XMLElement, let key = Self.crsName(element) else { continue }
            let items = Self.listItems(element)
            switch key {
            case "Name":
                name = items.first?.stringValue ?? ""
            case "Look":
                let look = Self.elements(element).first { Self.isRDF($0, "Description") }
                self.look = look.flatMap { Self.crsAttributes($0)["Name"] }
            case "LensBlur":
                lensBlurActive = Self.crsAttributes(element)["Active"]?.lowercased() == "true"
            case "MaskGroupBasedCorrections", "CircularGradientBasedCorrections", "GradientBasedCorrections", "PaintBasedCorrections":
                for item in items { corrections.append(Self.correction(item, kind: key)) }
            default:
                if !items.isEmpty {
                    lists[key] = items.compactMap { $0.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) }
                } else if Self.elements(element).isEmpty, let text = element.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) {
                    values[key] = text
                }
            }
        }
    }

    private static func crsName(_ node: XMLNode) -> String? {
        guard let name = node.name else { return nil }
        if node.uri == XMPNamespace.crs || name.hasPrefix("crs:") { return node.localName ?? String(name.dropFirst(4)) }
        return nil
    }
    private static func isRDF(_ node: XMLNode, _ local: String) -> Bool {
        node.localName == local && (node.name?.hasPrefix("rdf:") == true || node.uri?.contains("rdf-syntax") == true)
    }
    private static func elements(_ element: XMLElement) -> [XMLElement] {
        (element.children ?? []).compactMap { $0 as? XMLElement }
    }
    private static func crsAttributes(_ element: XMLElement) -> [String: String] {
        var result: [String: String] = [:]
        for attribute in element.attributes ?? [] {
            if let key = crsName(attribute) { result[key] = attribute.stringValue ?? "" }
        }
        return result
    }
    /// The rdf:li items of an rdf:Seq / rdf:Alt / rdf:Bag directly inside `element`.
    private static func listItems(_ element: XMLElement) -> [XMLElement] {
        elements(element).filter { isRDF($0, "Seq") || isRDF($0, "Alt") || isRDF($0, "Bag") }
            .flatMap { elements($0).filter { isRDF($0, "li") } }
    }
    /// An item's attributes, whether written on the rdf:li or on an rdf:Description inside it.
    private static func itemAttributes(_ item: XMLElement) -> [String: String] {
        var result = crsAttributes(item)
        for description in elements(item) where isRDF(description, "Description") {
            result.merge(crsAttributes(description)) { first, _ in first }
        }
        return result
    }
    private static func correction(_ item: XMLElement, kind: String) -> LocalCorrection {
        let attributes = itemAttributes(item)
        var settings: [String: Double] = [:]
        for (key, value) in attributes where key.hasPrefix("Local") {
            if let number = Double(value) { settings[key] = number }
        }
        let holder = elements(item).first { isRDF($0, "Description") } ?? item
        var masks: [[String: String]] = []
        for child in elements(holder) where crsName(child) == "CorrectionMasks" {
            masks += listItems(child).map(itemAttributes)
        }
        // Older presets put a single gradient's geometry on the correction itself.
        if masks.isEmpty, kind != "MaskGroupBasedCorrections" {
            var mask = attributes
            mask["What"] = kind == "GradientBasedCorrections" ? "Mask/Gradient" : kind == "CircularGradientBasedCorrections" ? "Mask/CircularGradient" : "Mask/Paint"
            masks = [mask]
        }
        return LocalCorrection(name: attributes["CorrectionName"] ?? "Local correction",
                               active: attributes["CorrectionActive"]?.lowercased() != "false",
                               amount: Double(attributes["CorrectionAmount"] ?? "1") ?? 1,
                               settings: settings, masks: masks)
    }
}

/// Camera Raw fields by the names `--set` and reports use.
enum CameraRawFields {
    static let paths: [String: WritableKeyPath<CameraRawSettings, Double>] = {
        var paths: [String: WritableKeyPath<CameraRawSettings, Double>] = [
            "exposure": \.exposure, "contrast": \.contrast, "highlights": \.highlights, "shadows": \.shadows,
            "whites": \.whites, "blacks": \.blacks, "temperature": \.temperature, "tint": \.tint,
            "vibrance": \.vibrance, "saturation": \.saturation, "texture": \.texture, "clarity": \.clarity,
            "dehaze": \.dehaze, "glow": \.glow, "vignetteAmount": \.vignetteAmount, "vignetteMidpoint": \.vignetteMidpoint,
            "vignetteRoundness": \.vignetteRoundness, "vignetteFeather": \.vignetteFeather,
            "vignetteHighlights": \.vignetteHighlights, "grainAmount": \.grainAmount, "grainSize": \.grainSize,
            "grainRoughness": \.grainRoughness,
            "curve.shadows": \.curve.shadows, "curve.darks": \.curve.darks, "curve.lights": \.curve.lights,
            "curve.highlights": \.curve.highlights, "curve.shadowSplit": \.curve.shadowSplit,
            "curve.darkSplit": \.curve.darkSplit, "curve.lightSplit": \.curve.lightSplit,
            "curve.refineSaturation": \.curve.refineSaturation,
            "grading.blending": \.grading.blending, "grading.balance": \.grading.balance,
            "detail.sharpenAmount": \.detail.sharpenAmount, "detail.sharpenRadius": \.detail.sharpenRadius,
            "detail.sharpenDetail": \.detail.sharpenDetail, "detail.sharpenMasking": \.detail.sharpenMasking,
            "detail.noiseLuminance": \.detail.noiseLuminance, "detail.noiseLuminanceDetail": \.detail.noiseLuminanceDetail,
            "detail.noiseLuminanceContrast": \.detail.noiseLuminanceContrast, "detail.noiseColor": \.detail.noiseColor,
            "detail.noiseColorDetail": \.detail.noiseColorDetail, "detail.noiseColorSmoothness": \.detail.noiseColorSmoothness,
            "optics.distortion": \.optics.distortion, "optics.purpleAmount": \.optics.purpleAmount,
            "optics.greenAmount": \.optics.greenAmount, "optics.vignetteAmount": \.optics.vignetteAmount,
            "optics.vignetteMidpoint": \.optics.vignetteMidpoint,
            "geometry.vertical": \.geometry.vertical, "geometry.horizontal": \.geometry.horizontal,
            "geometry.rotate": \.geometry.rotate, "geometry.aspect": \.geometry.aspect, "geometry.scale": \.geometry.scale,
            "geometry.offsetX": \.geometry.offsetX, "geometry.offsetY": \.geometry.offsetY,
            "calibration.shadowTint": \.calibration.shadowTint, "calibration.redHue": \.calibration.redHue,
            "calibration.redSaturation": \.calibration.redSaturation, "calibration.greenHue": \.calibration.greenHue,
            "calibration.greenSaturation": \.calibration.greenSaturation, "calibration.blueHue": \.calibration.blueHue,
            "calibration.blueSaturation": \.calibration.blueSaturation,
        ]
        for (index, band) in mixerBands.enumerated() {
            paths["mixer.hue.\(band)"] = \.mixer.hue[index]
            paths["mixer.saturation.\(band)"] = \.mixer.saturation[index]
            paths["mixer.luminance.\(band)"] = \.mixer.luminance[index]
        }
        for wheel in ["shadows", "midtones", "highlights", "global"] {
            let path: WritableKeyPath<CameraRawSettings, CameraRawGradeWheel> = switch wheel {
            case "shadows": \.grading.shadows
            case "midtones": \.grading.midtones
            case "highlights": \.grading.highlights
            default: \.grading.global
            }
            paths["grading.\(wheel).hue"] = path.appending(path: \.hue)
            paths["grading.\(wheel).saturation"] = path.appending(path: \.saturation)
            paths["grading.\(wheel).luminance"] = path.appending(path: \.luminance)
        }
        return paths
    }()
    /// Lightroom's color mixer bands, in Compositor's order.
    static let mixerBands = ["red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"]
}

/// A preset read onto Camera Raw settings, with a record of what went where.
struct PresetMapping {
    struct Entry: Encodable {
        let from: String, to: String, value: Double
        /// A curve's points (x, y on 0–255), when the entry is a point curve; `value` is then their count.
        var points: [[Double]]? = nil
    }
    struct WhiteBalance: Encodable {
        let target: [Double]
        let reference: [Double]
        let referenceIs: String
        let temperature: Double
        let tint: Double
    }

    var settings = CameraRawSettings()
    var mapped: [Entry] = []
    var notes: [String] = []
    var skipped: [String] = []
    var whiteBalance: WhiteBalance?

    /// `amount` scales the whole preset (1 as saved, 0.5 half strength, up to 2). `asShot` is the photo's own white
    /// balance when known (RAW files): the preset's Kelvin is then reached from it, as Lightroom does.
    init(_ preset: LightroomPreset, amount: Double, asShot: (temperature: Double, tint: Double)?) {
        let v = preset.values
        func number(_ key: String) -> Double? { v[key].flatMap { Double($0.trimmingCharacters(in: .whitespaces)) } }
        func set(_ key: String, _ field: String, _ convert: (Double) -> Double = { $0 }) {
            guard let raw = number(key), let path = CameraRawFields.paths[field] else { return }
            let neutral = CameraRawSettings()[keyPath: path]
            let value = neutral + amount * (convert(raw) - neutral)
            if value != neutral || raw != 0 {
                settings[keyPath: path] = value
                if value != neutral { mapped.append(Entry(from: key, to: field, value: value)) }
            }
        }

        // Light, presence and color.
        for (key, field) in [("Exposure2012", "exposure"), ("Contrast2012", "contrast"), ("Highlights2012", "highlights"),
                             ("Shadows2012", "shadows"), ("Whites2012", "whites"), ("Blacks2012", "blacks"),
                             ("Texture", "texture"), ("Clarity2012", "clarity"), ("Dehaze", "dehaze"),
                             ("Vibrance", "vibrance"), ("Saturation", "saturation")] {
            set(key, field)
        }
        // Process 2010 and older presets.
        if v["Exposure2012"] == nil { set("Exposure", "exposure") }
        if v["Contrast2012"] == nil { set("Contrast", "contrast") }
        if v["Clarity2012"] == nil { set("Clarity", "clarity") }

        // White balance.
        let mode = v["WhiteBalance"] ?? ""
        if mode == "Auto" {
            settings.whiteBalance = .auto
            notes.append("White balance Auto: Compositor's gray-world auto, not Adobe's.")
        } else if number("IncrementalTemperature") != nil || number("IncrementalTint") != nil {
            set("IncrementalTemperature", "temperature")
            set("IncrementalTint", "tint")
            notes.append("Relative white balance (for JPEGs) mapped directly; Compositor's warm/cool scale is its own.")
        } else if mode != "As Shot", let kelvin = number("Temperature") {
            let tint = number("Tint") ?? 0
            let reference: (Double, Double, String)? = asShot.map { ($0.temperature, $0.tint, "this photo's as-shot") }
                ?? number("AsShotTemperature").map { ($0, number("AsShotTint") ?? 0, "the preset's own photo, as shot") }
            if let reference {
                let target = WhiteBalanceShift.blend(from: (reference.0, reference.1), to: (kelvin, tint), amount: amount)
                let slider = WhiteBalanceShift.sliders(from: (reference.0, reference.1), to: target)
                settings.temperature = slider.temperature
                settings.tint = slider.tint
                whiteBalance = WhiteBalance(target: [kelvin, tint], reference: [reference.0, reference.1], referenceIs: reference.2,
                                            temperature: slider.temperature, tint: slider.tint)
                mapped.append(Entry(from: "Temperature/Tint", to: "temperature", value: slider.temperature))
                mapped.append(Entry(from: "Temperature/Tint", to: "tint", value: slider.tint))
                if asShot == nil {
                    notes.append("White balance \(Int(kelvin)) K / \(Int(tint)) is absolute; for a non-RAW photo it is applied as the "
                        + "shift it made on the preset's own photo (as shot \(Int(reference.0)) K / \(Int(reference.1))).")
                }
            } else {
                skipped.append("White balance \(Int(kelvin)) K / \(Int(tint)): absolute, and the preset has no as-shot reference")
            }
        }

        // Tone curve: parametric regions and point curves (0–255 in XMP, 0–1 here).
        for (key, field) in [("ParametricShadows", "curve.shadows"), ("ParametricDarks", "curve.darks"),
                             ("ParametricLights", "curve.lights"), ("ParametricHighlights", "curve.highlights"),
                             ("ParametricShadowSplit", "curve.shadowSplit"), ("ParametricMidtoneSplit", "curve.darkSplit"),
                             ("ParametricHighlightSplit", "curve.lightSplit")] {
            set(key, field)
        }
        let curves: [(String, String, WritableKeyPath<CameraRawCurveSettings, [CurvePoint]>)] = [
            ("ToneCurvePV2012", "curve.rgb", \.rgb), ("ToneCurvePV2012Red", "curve.red", \.red),
            ("ToneCurvePV2012Green", "curve.green", \.green), ("ToneCurvePV2012Blue", "curve.blue", \.blue)]
        for (key, field, path) in curves {
            guard let items = preset.lists[key] ?? (key == "ToneCurvePV2012" ? preset.lists["ToneCurve"] : nil) else { continue }
            let points = items.compactMap { item -> CurvePoint? in
                let parts = item.split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
                guard parts.count == 2 else { return nil }
                let x = parts[0] / 255, y = parts[1] / 255
                return CurvePoint(x: x, y: x + amount * (y - x))
            }
            if points.count >= 2, !CameraRawCurveSettings.isLinear(points) {
                settings.curve[keyPath: path] = points
                mapped.append(Entry(from: key, to: field, value: Double(points.count),
                                    points: points.map { [($0.x * 255).rounded(), ($0.y * 255 * 10).rounded() / 10] }))
            }
        }

        // Color mixer.
        let lightroomBands = ["Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta"]
        for (band, name) in zip(CameraRawFields.mixerBands, lightroomBands) {
            set("HueAdjustment\(name)", "mixer.hue.\(band)")
            set("SaturationAdjustment\(name)", "mixer.saturation.\(band)")
            set("LuminanceAdjustment\(name)", "mixer.luminance.\(band)")
        }
        if v["ConvertToGrayscale"]?.lowercased() == "true" {
            settings.saturation = -100 * amount
            mapped.append(Entry(from: "ConvertToGrayscale", to: "saturation", value: settings.saturation))
            if v.keys.contains(where: { $0.hasPrefix("GrayMixer") }) {
                notes.append("Black & white: made by removing saturation; the B&W mix (GrayMixer…) has no counterpart.")
            }
        }

        // Color grading (Lightroom keeps the shadow and highlight hue/saturation under its split-toning names).
        set("SplitToningShadowHue", "grading.shadows.hue"); set("SplitToningShadowSaturation", "grading.shadows.saturation")
        set("SplitToningHighlightHue", "grading.highlights.hue"); set("SplitToningHighlightSaturation", "grading.highlights.saturation")
        set("SplitToningBalance", "grading.balance")
        for (lightroom, wheel) in [("Shadow", "shadows"), ("Midtone", "midtones"), ("Highlight", "highlights"), ("Global", "global")] {
            set("ColorGrade\(lightroom)Hue", "grading.\(wheel).hue")
            set("ColorGrade\(lightroom)Sat", "grading.\(wheel).saturation")
            set("ColorGrade\(lightroom)Lum", "grading.\(wheel).luminance")
        }
        set("ColorGradeBlending", "grading.blending")
        if v["SplitToningBalance"] == nil { set("ColorGradeBalance", "grading.balance") }

        // Detail.
        set("Sharpness", "detail.sharpenAmount")
        set("SharpenRadius", "detail.sharpenRadius") { ($0 - 0.5) / 2.5 * 100 }  // 0.5–3.0 px onto Compositor's 0–100
        set("SharpenDetail", "detail.sharpenDetail")
        set("SharpenEdgeMasking", "detail.sharpenMasking")
        set("LuminanceSmoothing", "detail.noiseLuminance")
        set("LuminanceNoiseReductionDetail", "detail.noiseLuminanceDetail")
        set("LuminanceNoiseReductionContrast", "detail.noiseLuminanceContrast")
        set("ColorNoiseReduction", "detail.noiseColor")
        set("ColorNoiseReductionDetail", "detail.noiseColorDetail")
        set("ColorNoiseReductionSmoothness", "detail.noiseColorSmoothness")

        // Effects.
        set("PostCropVignetteAmount", "vignetteAmount")
        if (number("PostCropVignetteAmount") ?? 0) != 0 {
            set("PostCropVignetteMidpoint", "vignetteMidpoint")
            set("PostCropVignetteRoundness", "vignetteRoundness")
            set("PostCropVignetteFeather", "vignetteFeather")
            set("PostCropVignetteHighlightContrast", "vignetteHighlights")
            switch number("PostCropVignetteStyle") {
            case 2: settings.vignetteStyle = .colorPriority
            case 3: settings.vignetteStyle = .paintOverlay
            default: settings.vignetteStyle = .highlightPriority
            }
        }
        set("GrainAmount", "grainAmount")
        if (number("GrainAmount") ?? 0) != 0 {
            set("GrainSize", "grainSize")
            set("GrainFrequency", "grainRoughness")
        }

        // Optics and geometry.
        if v["AutoLateralCA"] == "1" { settings.optics.removeChromaticAberration = true; mapped.append(Entry(from: "AutoLateralCA", to: "optics.removeChromaticAberration", value: 1)) }
        set("LensManualDistortionAmount", "optics.distortion")
        set("VignetteAmount", "optics.vignetteAmount")
        if (number("VignetteAmount") ?? 0) != 0 { set("VignetteMidpoint", "optics.vignetteMidpoint") }
        set("DefringePurpleAmount", "optics.purpleAmount")
        set("DefringeGreenAmount", "optics.greenAmount")
        if (number("DefringePurpleAmount") ?? 0) != 0 || (number("DefringeGreenAmount") ?? 0) != 0 {
            notes.append("Defringe amounts mapped; their hue ranges keep Compositor's defaults.")
        }
        if v["LensProfileEnable"] == "1" {
            skipped.append("Lens profile corrections: need the lens's own profile data, which Compositor doesn't have")
        }
        if let upright = number("PerspectiveUpright"), upright != 0 {
            skipped.append("Upright (automatic perspective): Compositor has guided upright only")
        }
        set("PerspectiveVertical", "geometry.vertical"); set("PerspectiveHorizontal", "geometry.horizontal")
        set("PerspectiveRotate", "geometry.rotate"); set("PerspectiveAspect", "geometry.aspect")
        set("PerspectiveScale", "geometry.scale") { $0 - 100 }
        set("PerspectiveX", "geometry.offsetX"); set("PerspectiveY", "geometry.offsetY")

        // Calibration.
        for (key, field) in [("ShadowTint", "calibration.shadowTint"), ("RedHue", "calibration.redHue"),
                             ("RedSaturation", "calibration.redSaturation"), ("GreenHue", "calibration.greenHue"),
                             ("GreenSaturation", "calibration.greenSaturation"), ("BlueHue", "calibration.blueHue"),
                             ("BlueSaturation", "calibration.blueSaturation")] {
            set(key, field)
        }

        // What has no counterpart.
        if let look = preset.look, !look.isEmpty {
            skipped.append("Profile \"\(look)\": Adobe's camera profiles and looks aren't available outside Adobe's apps")
        }
        if v.keys.contains(where: { $0.hasPrefix("RGBTable") || $0.hasPrefix("LookTable") || $0.hasPrefix("Table_") }) {
            skipped.append("Color lookup table (RGBTable/LookTable)")
        }
        if preset.lensBlurActive { skipped.append("Lens Blur") }
        if v["HasCrop"]?.lowercased() == "true" { skipped.append("Crop: the layout decides the crop instead") }
        if let point = preset.lists["PointColors"], point.contains(where: { !$0.hasPrefix("-1") }) {
            skipped.append("Point Color: not mapped yet")
        }
        let handled: Set<String> = [
            "Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012", "Texture", "Clarity2012",
            "Dehaze", "Vibrance", "Saturation", "Exposure", "Contrast", "Clarity", "WhiteBalance", "Temperature", "Tint",
            "IncrementalTemperature", "IncrementalTint", "AsShotTemperature", "AsShotTint", "ParametricShadows", "ParametricDarks",
            "ParametricLights", "ParametricHighlights", "ParametricShadowSplit", "ParametricMidtoneSplit", "ParametricHighlightSplit",
            "ConvertToGrayscale", "SplitToningShadowHue", "SplitToningShadowSaturation", "SplitToningHighlightHue",
            "SplitToningHighlightSaturation", "SplitToningBalance", "ColorGradeBlending", "ColorGradeBalance", "Sharpness",
            "SharpenRadius", "SharpenDetail", "SharpenEdgeMasking", "LuminanceSmoothing", "LuminanceNoiseReductionDetail",
            "LuminanceNoiseReductionContrast", "ColorNoiseReduction", "ColorNoiseReductionDetail", "ColorNoiseReductionSmoothness",
            "PostCropVignetteAmount", "PostCropVignetteMidpoint", "PostCropVignetteRoundness", "PostCropVignetteFeather",
            "PostCropVignetteHighlightContrast", "PostCropVignetteStyle", "GrainAmount", "GrainSize", "GrainFrequency",
            "AutoLateralCA", "LensManualDistortionAmount", "VignetteAmount", "VignetteMidpoint", "DefringePurpleAmount",
            "DefringeGreenAmount", "DefringePurpleHueLo", "DefringePurpleHueHi", "DefringeGreenHueLo", "DefringeGreenHueHi",
            "LensProfileEnable", "PerspectiveUpright", "PerspectiveVertical", "PerspectiveHorizontal", "PerspectiveRotate",
            "PerspectiveAspect", "PerspectiveScale", "PerspectiveX", "PerspectiveY", "ShadowTint", "RedHue", "RedSaturation",
            "GreenHue", "GreenSaturation", "BlueHue", "BlueSaturation", "HasCrop",
        ]
        let metadata: Set<String> = [
            "PresetType", "Cluster", "UUID", "SupportsAmount", "SupportsAmount2", "SupportsColor", "SupportsMonochrome",
            "SupportsHighDynamicRange", "SupportsNormalDynamicRange", "SupportsSceneReferred", "SupportsOutputReferred",
            "RequiresRGBTables", "CameraModelRestriction", "Copyright", "ContactInfo", "Version", "CompatibleVersion",
            "ProcessVersion", "HasSettings", "ToneCurveName", "ToneCurveName2010", "ToneCurveName2012", "LensProfileSetup",
            "OverrideLookVignette", "CropConstrainToWarp", "AlreadyApplied", "CropTop", "CropLeft", "CropBottom", "CropRight",
            "CropAngle", "CropConstrainToUnitSquare", "HDREditMode", "HDRMaxValue", "SDRBrightness", "SDRContrast",
            "SDRHighlights", "SDRShadows", "SDRWhites", "SDRBlend", "Name", "ShortName", "SortName", "Group", "Description",
            "LensProfileName", "LensProfileFilename", "LensProfileDigest", "LensProfileIsEmbedded", "LensProfileDistortionScale",
            "LensProfileVignettingScale", "LensProfileChromaticAberrationScale", "CameraProfile", "CameraProfileDigest",
        ]
        for (key, value) in v.sorted(by: { $0.key < $1.key }) where !handled.contains(key) && !metadata.contains(key) {
            let neutral = Double(value) == 0 || ["", "false", "False"].contains(value)
            if !neutral { skipped.append("\(key) = \(value): no counterpart") }
        }
    }
}

/// White balance in Kelvin and tint, turned into the relative warm/cool and green/magenta gains Compositor's Camera
/// Raw applies in linear light. Whites come from the Planckian locus (Kim et al.); tint moves the assumed light off it
/// by 1/3000 in uv per step (the DNG SDK's scale), toward green for positive tint, which is why a positive tint turns
/// the picture magenta; and the channel ratio between the two whites is solved onto Compositor's gain formula
/// (CameraRawSettings.gains).
enum WhiteBalanceShift {
    static func blend(from: (Double, Double), to: (Double, Double), amount: Double) -> (Double, Double) {
        // In mireds, where equal steps look equal.
        let fromMired = 1e6 / from.0, toMired = 1e6 / to.0
        return (1e6 / (fromMired + amount * (toMired - fromMired)), from.1 + amount * (to.1 - from.1))
    }

    static func sliders(from reference: (Double, Double), to target: (Double, Double)) -> (temperature: Double, tint: Double) {
        let a = white(reference.0, reference.1), b = white(target.0, target.1)
        // Neutralizing for `target` instead of `reference`: multiply by reference white / target white.
        let gain = (a.0 / b.0, a.1 / b.1, a.2 / b.2)
        let red = gain.0 / gain.1, blue = gain.2 / gain.1
        let warmGain = CameraRawSettings.temperatureGain, tintRB = CameraRawSettings.tintRedBlue, tintG = CameraRawSettings.tintGreen
        let magenta = ((red + blue) - 2) / (2 * tintRB + (red + blue) * tintG)
        let warm = (red - blue) * (1 - tintG * magenta) / (2 * warmGain)
        return (max(-100, min(100, warm * 100)), max(-100, min(100, magenta * 100)))
    }

    /// Linear sRGB of a white lit at `kelvin`, nudged off the locus by `tint`.
    static func white(_ kelvin: Double, _ tint: Double) -> (Double, Double, Double) {
        let t = max(1700, min(25000, kelvin))
        func uv(_ t: Double) -> (Double, Double) {
            let (x, y) = locus(t)
            let d = -2 * x + 12 * y + 3
            return (4 * x / d, 6 * y / d)
        }
        var (u, v) = uv(t)
        let ahead = uv(1e6 / (1e6 / t - 1)), behind = uv(1e6 / (1e6 / t + 1))
        var normal = (-(ahead.1 - behind.1), ahead.0 - behind.0)
        let length = hypot(normal.0, normal.1)
        normal = (normal.0 / length, normal.1 / length)
        if normal.1 < 0 { normal = (-normal.0, -normal.1) }  // toward green
        let duv = tint / 3000
        u += duv * normal.0; v += duv * normal.1
        let d = 2 * u - 8 * v + 4
        let x = 3 * u / d, y = 2 * v / d
        let X = x / y, Y = 1.0, Z = (1 - x - y) / y
        return (3.2406 * X - 1.5372 * Y - 0.4986 * Z, -0.9689 * X + 1.8758 * Y + 0.0415 * Z, 0.0557 * X - 0.2040 * Y + 1.0570 * Z)
    }

    private static func locus(_ t: Double) -> (Double, Double) {
        let t2 = t * t, t3 = t2 * t
        let x = t <= 4000
            ? -0.2661239e9 / t3 - 0.2343589e6 / t2 + 0.8776956e3 / t + 0.179910
            : -3.0258469e9 / t3 + 2.1070379e6 / t2 + 0.2226347e3 / t + 0.240390
        let x2 = x * x, x3 = x2 * x
        let y = t <= 2222 ? -1.1063814 * x3 - 1.34811020 * x2 + 2.18555832 * x - 0.20219683
            : t <= 4000 ? -0.9549476 * x3 - 1.37418593 * x2 + 2.09137015 * x - 0.16748867
            : 3.0817580 * x3 - 5.87338670 * x2 + 3.75112997 * x - 0.37001483
        return (x, y)
    }
}
