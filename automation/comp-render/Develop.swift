import Foundation
import CoreGraphics
import CoreImage

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
    /// For a RAW file: what the RAW decode applies, before the picture becomes 8-bit.
    var raw: RawStage?
    var mapped: [Entry] = []
    var notes: [String] = []
    var skipped: [String] = []
    var whiteBalance: WhiteBalance?

    /// `amount` scales the whole preset (1 as saved, 0.5 half strength, up to 2). `asShot` is the photo's own white
    /// balance when known: the preset's Kelvin is then reached from it, as Lightroom does. With `rawStage`, the
    /// picture is a RAW file still to be decoded, and exposure, white balance (in Kelvin, exactly), highlights,
    /// whites and lens corrections go to the RAW decode instead of the 8-bit engine.
    init(_ preset: LightroomPreset, amount: Double, asShot: (temperature: Double, tint: Double)?, rawStage: Bool = false) {
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
        var engineKeys = [("Exposure2012", "exposure"), ("Contrast2012", "contrast"), ("Highlights2012", "highlights"),
                          ("Shadows2012", "shadows"), ("Whites2012", "whites"), ("Blacks2012", "blacks"),
                          ("Texture", "texture"), ("Clarity2012", "clarity"), ("Dehaze", "dehaze"),
                          ("Vibrance", "vibrance"), ("Saturation", "saturation")]
        if rawStage {
            var stage = RawStage()
            stage.exposure = amount * (number("Exposure2012") ?? number("Exposure") ?? 0)
            stage.highlights = amount * (number("Highlights2012") ?? 0)
            stage.whites = amount * (number("Whites2012") ?? 0)
            raw = stage
            engineKeys.removeAll { ["Exposure2012", "Highlights2012", "Whites2012"].contains($0.0) }
            for (key, field, value) in [("Exposure2012", "raw.exposure", stage.exposure), ("Highlights2012", "raw.highlights", stage.highlights),
                                        ("Whites2012", "raw.whites", stage.whites)] where value != 0 {
                mapped.append(Entry(from: key, to: field, value: value))
            }
        }
        for (key, field) in engineKeys {
            set(key, field)
        }
        // Process 2010 and older presets.
        if v["Exposure2012"] == nil && !rawStage { set("Exposure", "exposure") }
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
        } else if mode != "As Shot", let kelvin = number("Temperature"), rawStage, let asShot {
            let tint = number("Tint") ?? 0
            let target = WhiteBalanceShift.blend(from: (asShot.temperature, asShot.tint), to: (kelvin, tint), amount: amount)
            raw?.temperature = target.0
            raw?.tint = target.1
            mapped.append(Entry(from: "Temperature", to: "raw.temperature", value: target.0))
            mapped.append(Entry(from: "Tint", to: "raw.tint", value: target.1))
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
            if rawStage {
                raw?.lensCorrection = true
                mapped.append(Entry(from: "LensProfileEnable", to: "raw.lensCorrection", value: 1))
                notes.append("Lens corrections: from the camera's own correction data in the RAW, not Adobe's lens profile.")
            } else {
                skipped.append("Lens profile corrections: need the lens's own profile data, which Compositor doesn't have")
            }
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

/// The part of a grade done while decoding a RAW file: what Lightroom applies to the sensor data itself. Apple's RAW
/// pipeline works in floating point on all 14 bits, so exposure and white balance move real captured light, and
/// highlights above the camera's white are still there to roll off rather than already clipped.
struct RawStage: Encodable {
    /// Kelvin and tint to neutralize, as Lightroom's Temperature/Tint; nil keeps the camera's as-shot reading.
    var temperature: Double?
    var tint: Double?
    var exposure: Double = 0
    /// Lightroom's −100…100: negative rolls the brightest tones down into range; positive opens them up.
    var highlights: Double = 0
    var whites: Double = 0
    var lensCorrection = false
}

/// Decodes a RAW file: cropped and sized in the RAW pipeline, with a `RawStage` applied in floating point, and only
/// then turned into the 8-bit sRGB picture Compositor's layers hold.
struct RawDeveloper {
    /// Tuning, settable from comp-render's calibration flags; defaults were chosen against Lightroom exports.
    struct Tuning {
        var boost = 1.0
        var headroom = 0.0
        var sharpness: Double? = 0
        var noise: Double? = nil
        var tintScale = 1.0
        var shoulder = 0.3
        /// How far Highlights −100 darkens the upper tones (a fraction of their level, most around light midtones).
        var highlightStrength = 0.4
    }

    let filter: CIRAWFilter
    let asShot: (temperature: Double, tint: Double)
    /// The picture's upright size at full resolution.
    let upright: CGSize
    var tuning = Tuning()
    private static let context = CIContext(options: [.useSoftwareRenderer: false,
        .workingColorSpace: CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!])

    init?(url: URL) {
        guard let filter = CIRAWFilter(imageURL: url), let image = filter.outputImage else { return nil }
        self.filter = filter
        asShot = (Double(filter.neutralTemperature), Double(filter.neutralTint))
        upright = image.extent.size
    }

    /// `stage` nil develops the picture as shot (the kept original). `rotate` levels it first (degrees,
    /// counterclockwise; see `Straighten`). `crop` is in fractions of the upright (and leveled) picture from its
    /// top-left; `size` the exact pixel size wanted.
    func render(_ stage: RawStage?, crop: CGRect?, size: (Int, Int)?, rotate: Double = 0) throws -> CGImage {
        let frame = crop ?? CGRect(x: 0, y: 0, width: 1, height: 1)
        let kept = Straighten.scale(width: upright.width, height: upright.height, degrees: rotate)
        var scale = 1.0
        if let size {
            scale = min(1, max(Double(size.0) / (frame.width * upright.width * kept),
                               Double(size.1) / (frame.height * upright.height * kept)) * 1.05)
        }
        filter.scaleFactor = Float(scale)
        filter.isDraftModeEnabled = false
        filter.boostAmount = Float(tuning.boost)
        filter.extendedDynamicRangeAmount = Float(stage == nil ? 0 : tuning.headroom)
        if filter.isHighlightRecoverySupported { filter.isHighlightRecoveryEnabled = true }
        if let sharpness = tuning.sharpness, filter.isSharpnessSupported { filter.sharpnessAmount = Float(sharpness) }
        if let noise = tuning.noise, filter.isLuminanceNoiseReductionSupported { filter.luminanceNoiseReductionAmount = Float(noise) }
        if filter.isLensCorrectionSupported { filter.isLensCorrectionEnabled = stage?.lensCorrection ?? true }
        filter.neutralTemperature = Float(stage?.temperature ?? asShot.temperature)
        filter.neutralTint = Float(stage?.tint.map { $0 * tuning.tintScale } ?? asShot.tint)
        filter.exposure = Float(stage?.exposure ?? 0)
        guard var image = filter.outputImage else { throw ImageImportError.unreadable }
        if rotate != 0 { image = Straighten.apply(image, degrees: rotate) }
        // Fractions from the top-left onto Core Image's bottom-left extent.
        let extent = image.extent
        var rect = CGRect(x: extent.minX + frame.minX * extent.width,
                          y: extent.maxY - (frame.minY + frame.height) * extent.height,
                          width: frame.width * extent.width, height: frame.height * extent.height).integral.intersection(extent)
        var picture = image.cropped(to: rect).transformed(by: CGAffineTransform(translationX: -rect.minX, y: -rect.minY))
        var width = Int(rect.width), height = Int(rect.height)
        if let size, (size.0, size.1) != (width, height) {
            let sx = Double(size.0) / rect.width, sy = Double(size.1) / rect.height
            picture = picture.applyingFilter("CILanczosScaleTransform", parameters: [kCIInputScaleKey: sy, kCIInputAspectRatioKey: sx / sy])
            width = size.0; height = size.1
        }
        rect = CGRect(x: 0, y: 0, width: width, height: height)
        // Linear light, unclamped, so what lies above white is still there for the highlights to roll off.
        var linear = [Float](repeating: 0, count: width * height * 4)
        Self.context.render(picture, toBitmap: &linear, rowBytes: width * 16, bounds: rect, format: .RGBAf,
                            colorSpace: CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!)
        var bytes = [UInt8](repeating: 255, count: width * height * 4)
        let highlights = Float(stage?.highlights ?? 0) / 100, whites = Float(stage?.whites ?? 0) / 100
        // Above this, light the RAW still holds beyond white rolls off into range instead of clipping.
        let shape = RawShape(
            shapes: stage != nil,
            knee: 1 - Float(tuning.shoulder) * max(0, -highlights) * 0.3 - (stage != nil && tuning.headroom > 0 ? 0.1 : 0),
            lift: 0.25 * max(0, highlights), white: 1 + 0.15 * whites,
            pull: Float(tuning.highlightStrength) * max(0, -highlights))
        linear.withUnsafeBufferPointer { source in
            bytes.withUnsafeMutableBufferPointer { target in
                shape.run(source.baseAddress!, into: target.baseAddress!, pixels: width * height)
            }
        }
        guard let provider = CGDataProvider(data: Data(bytes) as CFData),
              let result = CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
                                   space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                   bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
                                   provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
        else { throw ExportError.render }
        return result
    }
}

/// Leveling a picture: turned about its center by `degrees` (counterclockwise as seen), then trimmed to the largest
/// centered rectangle of its own shape that the turned picture still fills, so no corner is empty.
nonisolated enum Straighten {
    /// The share of the picture's width (and height) that stays.
    static func scale(width: Double, height: Double, degrees: Double) -> Double {
        guard degrees != 0, width > 0, height > 0 else { return 1 }
        let angle = abs(degrees) * .pi / 180, c = cos(angle), s = sin(angle)
        return min(width / (width * c + height * s), height / (width * s + height * c))
    }

    static func apply(_ image: CIImage, degrees: Double) -> CIImage {
        let extent = image.extent
        let center = CGPoint(x: extent.midX, y: extent.midY)
        let turned = image.transformed(by: CGAffineTransform(translationX: center.x, y: center.y)
            .rotated(by: degrees * .pi / 180).translatedBy(x: -center.x, y: -center.y))
        let keep = scale(width: extent.width, height: extent.height, degrees: degrees)
        let rect = CGRect(x: center.x - extent.width * keep / 2, y: center.y - extent.height * keep / 2,
                          width: extent.width * keep, height: extent.height * keep).integral
        return turned.cropped(to: rect).transformed(by: CGAffineTransform(translationX: -rect.minX, y: -rect.minY))
    }

    /// For an 8-bit picture (JPEG, HEIC …), the same through Core Image.
    static func apply(_ picture: CGImage, degrees: Double) throws -> CGImage {
        guard degrees != 0 else { return picture }
        let leveled = apply(CIImage(cgImage: picture), degrees: degrees)
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        guard let result = CIContext(options: [.workingColorSpace: space]).createCGImage(leveled, from: leveled.extent,
                                                                                        format: .RGBA8, colorSpace: space)
        else { throw ExportError.render }
        return result
    }
}

/// The RAW stage's tone shaping from linear light to 8-bit sRGB: highlights, whites and the roll-off of what lies
/// above white. Each pixel depends on nothing but itself, so it runs in bands on every core.
nonisolated struct RawShape: Sendable {
    let shapes: Bool
    let knee: Float, lift: Float, white: Float, pull: Float

    static func encode(_ v: Float) -> Float { v <= 0.0031308 ? 12.92 * v : 1.055 * pow(v, 1 / 2.4) - 0.055 }
    static func decode(_ v: Float) -> Float { v <= 0.04045 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }

    func run(_ linear: UnsafePointer<Float>, into bytes: UnsafeMutablePointer<UInt8>, pixels: Int) {
        let bands = max(1, min(pixels / 65_536, ProcessInfo.processInfo.activeProcessorCount * 4))
        let share = (pixels + bands - 1) / bands
        let source = UnsafeSendable(linear), target = UnsafeSendable(bytes)
        DispatchQueue.concurrentPerform(iterations: bands) { band in
            for pixel in band * share..<min(pixels, (band + 1) * share) { shape(source.pointer, target.pointer, pixel * 4) }
        }
    }

    private func shape(_ linear: UnsafePointer<Float>, _ bytes: UnsafeMutablePointer<UInt8>, _ index: Int) {
        var r = max(0, linear[index]), g = max(0, linear[index + 1]), b = max(0, linear[index + 2])
        if shapes {
            // On the brightest channel, so a colored light keeps its hue as it's brought into range.
            let peak = max(r, g, b)
            if peak > 0 {
                var v = Self.encode(peak) * white
                // Lightroom's Highlights works on the upper half of the tones, most from light midtones up.
                if pull > 0 {
                    let t = min(1, max(0, (v - 0.12) / 0.7))
                    v *= 1 - pull * t * t * (3 - 2 * t)
                }
                if v > knee {
                    let room = max(0.0001, 1 - knee)
                    v = knee + room * (1 - exp(-(v - knee) / room))
                }
                if lift > 0 {
                    let t = min(1, max(0, (v - 0.5) / 0.5))
                    v += lift * t * t * (3 - 2 * t) * (1 - v)
                }
                let factor = Self.decode(min(1, v)) / peak
                r *= factor; g *= factor; b *= factor
            }
        }
        bytes[index] = UInt8(min(255, max(0, Self.encode(r) * 255 + 0.5)))
        bytes[index + 1] = UInt8(min(255, max(0, Self.encode(g) * 255 + 0.5)))
        bytes[index + 2] = UInt8(min(255, max(0, Self.encode(b) * 255 + 0.5)))
    }
}

/// A pointer handed to the threads of `concurrentPerform`, each of which writes only its own rows.
nonisolated struct UnsafeSendable<Pointer>: @unchecked Sendable {
    let pointer: Pointer
    init(_ pointer: Pointer) { self.pointer = pointer }
}

/// Runs a pointwise pixel kernel on horizontal bands on every core: only for kernels whose result at a pixel depends
/// on that pixel alone, so the output is byte for byte what one pass over the whole image makes.
nonisolated func inBands(_ pixels: UnsafeMutablePointer<UInt8>, height: Int, stride: Int,
                         _ body: @Sendable (UnsafeMutablePointer<UInt8>, Int) -> Void) {
    let bands = max(1, min(height / 16, ProcessInfo.processInfo.activeProcessorCount * 4))
    let rows = (height + bands - 1) / bands
    let base = UnsafeSendable(pixels)
    DispatchQueue.concurrentPerform(iterations: bands) { band in
        let first = band * rows
        if first < height { body(base.pointer + first * stride, min(rows, height - first)) }
    }
}

nonisolated extension CameraRawSettings {
    /// `apply` for a finished grade (no clipping overlay, point-color view or sharpen mask) with its pointwise stages
    /// (calibration, light and color, curve and color mixing) run in bands on every core. The stages that read
    /// neighbors or position (effects, grain, detail, optics, geometry) run exactly as the app runs them. The result is
    /// identical to `apply`'s; `comp-render develop --serial` runs the app's own to check that.
    func applyInParallel(_ image: CGImage, seed: UInt32 = 0) throws -> CGImage {
        let settings = normalized
        if settings.isIdentity { return image }
        guard settings.isValid else { throw ProjectError.invalid }
        let gains = settings.gains
        var source = image
        if settings.adjustsGeometry { source = try settings.geometry.apply(source) }
        return try ImageAdjustmentPixels.run(source) { pixels, width, height, stride in
            if settings.adjustsCalibration {
                inBands(pixels, height: height, stride: stride) { band, rows in
                    settings.applyCalibration(pixels: band, width: width, height: rows, stride: stride)
                }
            }
            if settings.adjustsLight || settings.adjustsColor {
                inBands(pixels, height: height, stride: stride) { band, rows in
                    adjust_camera_raw(band, width, rows, stride, gains.red, gains.green, gains.blue,
                                      settings.exposure, settings.contrast, settings.highlights, settings.shadows,
                                      settings.whites, settings.blacks, settings.vibrance, settings.saturation, 0)
                }
            }
            if settings.adjustsCurve || settings.adjustsMixer || settings.adjustsGrading {
                inBands(pixels, height: height, stride: stride) { band, rows in
                    settings.applyCurveColor(band, width: width, height: rows, stride: stride, visualize: -1)
                }
            }
            if settings.adjustsEffects {
                if settings.texture != 0 || settings.clarity != 0 || settings.dehaze != 0 || settings.glow != 0 || settings.vignetteAmount != 0 {
                    adjust_camera_raw_effects(pixels, width, height, stride,
                                              settings.texture, settings.clarity, settings.dehaze,
                                              settings.glow, settings.glowStyle.kernelValue, settings.glowRange,
                                              settings.glowSpread, settings.glowWarmth,
                                              settings.vignetteAmount, settings.vignetteMidpoint, settings.vignetteRoundness,
                                              settings.vignetteFeather, settings.vignetteHighlights, settings.vignetteStyle.kernelValue,
                                              1)
                }
                if settings.grainAmount > 0 {
                    adjust_grain(pixels, width, height, stride, settings.grainAmount, settings.grainKernelSize,
                                 settings.grainRoughness, seed, 0, 0, 1)
                }
            }
            if settings.adjustsDetail || settings.adjustsOptics {
                settings.applyDetailOptics(pixels: pixels, width: width, height: height, stride: stride, scale: 1,
                                           profileStrength: PixelFilter.lensStrength, sharpenMask: false)
            }
        }
    }
}

/// A 3D color lookup table (.cube, as Resolve, Lightroom and Photoshop write them), applied to an 8-bit sRGB picture.
struct ColorCube {
    let size: Int
    /// RGBA floats, red changing fastest, as Core Image's color cube takes them.
    let data: Data

    init(url: URL) throws {
        let text = try String(contentsOf: url, encoding: .utf8)
        var size = 0
        var values: [Float] = []
        for line in text.split(whereSeparator: \.isNewline) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") || trimmed.hasPrefix("TITLE") || trimmed.hasPrefix("DOMAIN") { continue }
            if trimmed.hasPrefix("LUT_3D_SIZE") {
                size = Int(trimmed.split(separator: " ").last ?? "") ?? 0
                continue
            }
            let parts = trimmed.split(separator: " ").compactMap { Float($0) }
            if parts.count == 3 { values += [parts[0], parts[1], parts[2], 1] }
        }
        guard (2...128).contains(size), values.count == size * size * size * 4 else { throw CocoaError(.fileReadCorruptFile) }
        self.size = size
        data = values.withUnsafeBufferPointer { Data(buffer: $0) }
    }

    func apply(_ image: CGImage) throws -> CGImage {
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let output = CIImage(cgImage: image).applyingFilter("CIColorCubeWithColorSpace", parameters: [
            "inputCubeDimension": size, "inputCubeData": data, "inputColorSpace": space, "inputExtrapolate": false])
        let context = CIContext(options: [.workingColorSpace: space, .useSoftwareRenderer: false])
        guard let result = context.createCGImage(output, from: CGRect(x: 0, y: 0, width: image.width, height: image.height),
                                                 format: .RGBA8, colorSpace: space) else { throw ExportError.render }
        return result
    }
}
