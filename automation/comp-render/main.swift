import AppKit
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
import Vision

// comp-render: Compositor's own loader, renderer and typesetter as a command-line tool, so scripts and agents can
// check, flatten and set type for .comp projects without the app open. Output matches the app's exactly.

let usage = """
usage:
  comp-render render <project.comp> <output.png|.jpg> [options]
      --quality <0-1>        JPEG quality (default 0.9)
      --max-side <pixels>    scale down so the longer side fits (default: full size)
      --background <RRGGBB>  JPEG background behind transparency (default FFFFFF)
  comp-render validate <project.comp>
      loads the project exactly as the app does; exit 0 if it would open, 1 with the reason if not
  comp-render info <project.comp>
      validates, then prints the manifest as JSON
  comp-render text <style.json> <output.png>
      sets a text layer's pixels with the app's typesetter from its `text` style (the manifest's text record) and
      prints {"width", "height", "padding", "baseline", "textWidth", "textHeight", "overflow"} as JSON: the PNG's
      size; for placing point text, how far the first baseline sits below the image's top edge and the text's left
      edge from its left edge; the size the set lines take; and whether they run past a paragraph box (cut off)
  comp-render fonts [text]
      installed fonts as "PostScriptName<TAB>Family Face" lines (the name a text style takes), optionally only
      those whose names contain the text
  comp-render defaults
      prints the app's default adjustment, effect and text records and its blend mode names as JSON
  comp-render cutout <picture> <output.png> [--mask] [--refine 0-40] [--contrast 0-100] [--shift -10-10] [--basic]
      Remove Background: the picture with its background transparent, or with --mask the grayscale subject mask
      (white over the subject) to use as a layer mask. Refines edges as the app's Advanced mode (refine 12,
      contrast 25, shift 0); --basic takes Vision's mask as it comes
  comp-render preset <preset.xmp>
      how a Lightroom / Camera Raw preset maps onto Compositor's Camera Raw filter, as JSON: what was mapped
      where, approximations, what has no counterpart, the white balance conversion and its local corrections
  comp-render develop <picture> <preset.xmp> <graded.png> [options]
      applies the preset with Compositor's Camera Raw engine. RAW files are decoded as shot first.
      --original <out.png>      also write the picture as decoded, cropped and sized but ungraded
      --crop <x,y,w,h>          crop first, in fractions of the upright picture (0–1)
      --size <WxH>              then resize to exactly this many pixels
      --amount <0-2>            preset strength (default 1)
      --as-shot <kelvin,tint>   the picture's own white balance, when it's known (RAW files report theirs)
      --set <field=value>       override a Camera Raw field after the preset (repeatable), e.g. exposure=0.3,
                                clarity=10, curve.shadows=5, mixer.saturation.orange=-10 (see `preset` output)
      --seed <n>                grain pattern (default 0)
      prints JSON: the as-shot white balance used, notes and what was skipped
  comp-render subject <picture>
      what Apple Vision finds in a picture, as JSON boxes in fractions of the picture from its top-left corner:
      faces, people (whole bodies), subject (the foreground Remove Background keeps, with its share of the
      picture) and salient (where the eye goes). Used to crop around what matters
"""

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func printJSON<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    guard let data = try? encoder.encode(value) else { fail("comp-render: could not encode JSON") }
    print(String(decoding: data, as: UTF8.self))
}

func load(_ path: String) async -> ProjectSnapshot {
    let url = URL(fileURLWithPath: path)
    do { return try await ProjectStore.shared.load(from: url) }
    catch { fail("comp-render: \(url.path): \(error.localizedDescription)") }
}

func write(_ image: CGImage, as type: UTType, to url: URL, properties: [CFString: Any] = [:]) {
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, type.identifier as CFString, 1, nil) else { fail("comp-render: could not encode") }
    CGImageDestinationAddImage(destination, image, properties as CFDictionary)
    guard CGImageDestinationFinalize(destination) else { fail("comp-render: could not encode") }
    do { try (data as Data).write(to: url, options: .atomic) }
    catch { fail("comp-render: \(url.path): \(error.localizedDescription)") }
}

/// A picture file as an imported picture is: upright (EXIF orientation applied) and drawn into sRGB, and with
/// `maxSide` scaled down so its longer side fits.
func loadPicture(_ path: String, maxSide: Int? = nil) -> CGImage {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let pixelWidth = properties[kCGImagePropertyPixelWidth] as? Int, let pixelHeight = properties[kCGImagePropertyPixelHeight] as? Int,
          let oriented = CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: min(maxSide ?? .max, max(pixelWidth, pixelHeight)),
            kCGImageSourceShouldCacheImmediately: true
          ] as CFDictionary),
          let space = CGColorSpace(name: CGColorSpace.sRGB),
          let canvas = CGContext(data: nil, width: oriented.width, height: oriented.height, bitsPerComponent: 8,
                                 bytesPerRow: oriented.width * 4, space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        fail("comp-render: \(url.path): not a picture macOS can read")
    }
    canvas.draw(oriented, in: CGRect(x: 0, y: 0, width: oriented.width, height: oriented.height))
    guard let picture = canvas.makeImage() else { fail("comp-render: \(url.path): could not read the picture") }
    return picture
}

func loadPreset(_ path: String) -> LightroomPreset {
    do { return try LightroomPreset(url: URL(fileURLWithPath: path)) }
    catch { fail("comp-render: \(path): not a Lightroom / Camera Raw preset (.xmp) this can read") }
}

var arguments = Array(CommandLine.arguments.dropFirst())
guard let command = arguments.first else { fail(usage) }
arguments.removeFirst()
// `comp-render a.comp out.png` is shorthand for render.
if command.hasSuffix(".comp") { arguments.insert(command, at: 0) }

switch command.hasSuffix(".comp") ? "render" : command {
case "-h", "--help", "help":
    print(usage)

case "validate":
    guard arguments.count == 1 else { fail(usage) }
    let snapshot = await load(arguments[0])
    print("ok \(snapshot.manifest.width)×\(snapshot.manifest.height), \(snapshot.manifest.layers.count) layers, format version \(snapshot.manifest.version)")

case "info":
    guard arguments.count == 1 else { fail(usage) }
    printJSON(await load(arguments[0]).manifest)

case "text":
    guard arguments.count == 2 else { fail(usage) }
    let styleURL = URL(fileURLWithPath: arguments[0])
    let style: LayerTextStyle
    do { style = try JSONDecoder().decode(LayerTextStyle.self, from: Data(contentsOf: styleURL)) }
    catch { fail("comp-render: \(styleURL.path): not a text style (\(error))") }
    guard style.isValid else { fail("comp-render: the text style is out of range (see LayerTextStyle.isValid)") }
    let image: CGImage
    do { image = try EditorSession.textImage(style) }
    catch { fail("comp-render: \(error.localizedDescription)") }
    write(image, as: .png, to: URL(fileURLWithPath: arguments[1]))
    // Where a click puts the first baseline, as the Type tool does: padding plus one line, less the font's descent.
    let descent = abs((EditorSession.textAttributes(style)[.font] as? NSFont)?.descender ?? 0)
    // The lines as the typesetter lays them out, unbounded in height, to tell whether a paragraph box cuts them off.
    let storage = NSTextStorage(attributedString: EditorSession.attributedText(style))
    let layout = NSLayoutManager()
    let container = NSTextContainer(size: CGSize(width: style.boxSize.map { max(1, $0.width - 2 * LayerTextStyle.padding) } ?? 100_000,
                                                 height: 100_000))
    container.lineFragmentPadding = 0
    storage.addLayoutManager(layout)
    layout.addTextContainer(container)
    layout.ensureLayout(for: container)
    let used = layout.usedRect(for: container)
    struct Placement: Encodable {
        let width: Int, height: Int, padding: Double, baseline: Double
        let textWidth: Double, textHeight: Double, overflow: Bool
    }
    printJSON(Placement(width: image.width, height: image.height, padding: LayerTextStyle.padding,
                        baseline: LayerTextStyle.padding + style.lineHeight - descent,
                        textWidth: used.width, textHeight: used.height,
                        overflow: style.boxSize.map { used.height > $0.height - 2 * LayerTextStyle.padding + 0.5 } ?? false))

case "fonts":
    let query = arguments.first?.lowercased()
    for family in NSFontManager.shared.availableFontFamilies {
        for member in NSFontManager.shared.availableMembers(ofFontFamily: family) ?? [] {
            guard let name = member.first as? String else { continue }
            let face = member.count > 1 ? (member[1] as? String ?? "") : ""
            let line = "\(name)\t\(family) \(face)"
            if let query, !line.lowercased().contains(query) { continue }
            print(line)
        }
    }

case "defaults":
    // The app's own new-record values, so writers start from exactly what its decoder expects: every non-optional
    // key present, every value in range.
    struct Effects: Encodable {
        let stroke = StrokeEffect(), shadow = ShadowEffect(), colorOverlay = ColorOverlayEffect()
        let innerShadow = InnerShadowEffect(), outerGlow = OuterGlowEffect(), innerGlow = InnerGlowEffect()
    }
    // Each kind with its own settings filled in at their defaults, so the full shape is visible. Hue/Saturation
    // keeps hsvSettings out: while it's missing, the record's plain hue, saturation and lightness apply.
    func filled(_ kind: AdjustmentKind) -> LayerAdjustment {
        var adjustment = LayerAdjustment(kind: kind)
        switch kind {
        case .exposure: adjustment.exposure = adjustment.exposure
        case .gradientMap: adjustment.gradientMap = adjustment.gradientMap
        case .grain: adjustment.grain = adjustment.grain
        case .blackWhite: adjustment.blackWhite = adjustment.blackWhite
        case .colorBalance: adjustment.colorBalance = adjustment.colorBalance
        case .gaussianBlur: adjustment.gaussianRadius = adjustment.gaussianRadius
        case .motionBlur:
            adjustment.resolvedMotionAngle = adjustment.resolvedMotionAngle
            adjustment.resolvedMotionDistance = adjustment.resolvedMotionDistance
        case .addNoise:
            adjustment.resolvedNoiseAmount = adjustment.resolvedNoiseAmount
            adjustment.resolvedNoiseGaussian = adjustment.resolvedNoiseGaussian
            adjustment.resolvedNoiseMonochromatic = adjustment.resolvedNoiseMonochromatic
            adjustment.resolvedNoiseSeed = adjustment.resolvedNoiseSeed
        case .hsv, .levels, .curves, .invert: break
        }
        return adjustment
    }
    struct Defaults: Encodable {
        let formatVersion = ProjectManifest.current
        let adjustments: [String: LayerAdjustment]
        let effects = Effects()
        let text = LayerTextStyle()
        let blendModes = LayerBlendMode.allCases.map(\.rawValue)
        let textPadding = LayerTextStyle.padding
    }
    printJSON(Defaults(adjustments: Dictionary(uniqueKeysWithValues: AdjustmentKind.allCases.map { ($0.rawValue, filled($0)) })))

case "cutout":
    // Remove Background's subject mask (Apple Vision, refined onto the picture's own edges) at full resolution.
    var positional: [String] = []
    var settings = FilterSettings()
    settings.backgroundQuality = .advanced
    var maskOnly = false
    while !arguments.isEmpty {
        let argument = arguments.removeFirst()
        func number(_ range: ClosedRange<Double>) -> Double {
            guard let text = arguments.first, let value = Double(text), range.contains(value) else {
                fail("\(argument) takes a number from \(range.lowerBound) to \(range.upperBound)")
            }
            arguments.removeFirst()
            return value
        }
        switch argument {
        case "--mask": maskOnly = true
        case "--basic": settings.backgroundQuality = .basic
        case "--refine": settings.refineEdges = number(0...40)
        case "--contrast": settings.matteContrast = number(0...100)
        case "--shift": settings.shiftEdge = number(-10...10)
        default: positional.append(argument)
        }
    }
    guard positional.count == 2 else { fail(usage) }
    let picture = loadPicture(positional[0])
    let bounds = CGRect(x: 0, y: 0, width: picture.width, height: picture.height)
    let mask: CGImage
    do { mask = try SubjectRemoval.subjectMask(picture, under: nil, settings: settings) }
    catch { fail("comp-render: \(error.localizedDescription)") }
    let outputURL = URL(fileURLWithPath: positional[1])
    if maskOnly {
        write(mask, as: .png, to: outputURL)
    } else {
        guard let canvas = CGContext(data: nil, width: picture.width, height: picture.height, bitsPerComponent: 8,
                                     bytesPerRow: picture.width * 4, space: picture.colorSpace ?? CGColorSpaceCreateDeviceRGB(),
                                     bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { fail("comp-render: could not render the cutout") }
        canvas.saveGState()
        canvas.clip(to: bounds, mask: mask)
        canvas.draw(picture, in: bounds)
        canvas.restoreGState()
        guard let cutout = canvas.makeImage() else { fail("comp-render: could not render the cutout") }
        write(cutout, as: .png, to: outputURL)
    }
    print("\(outputURL.path) \(mask.width)×\(mask.height)")

case "preset":
    guard arguments.count == 1 else { fail(usage) }
    let preset = loadPreset(arguments[0])
    let mapping = PresetMapping(preset, amount: 1, asShot: nil)
    struct Report: Encodable {
        let name: String, look: String?
        let mapped: [PresetMapping.Entry], whiteBalance: PresetMapping.WhiteBalance?
        let notes: [String], skipped: [String], localCorrections: [LightroomPreset.LocalCorrection]
        let fields: [String]
    }
    printJSON(Report(name: preset.name, look: preset.look, mapped: mapping.mapped, whiteBalance: mapping.whiteBalance,
                     notes: mapping.notes, skipped: mapping.skipped, localCorrections: preset.corrections,
                     fields: CameraRawFields.paths.keys.sorted()))

case "develop":
    var positional: [String] = []
    var originalPath: String?, crop: CGRect?, size: (Int, Int)?, amount = 1.0, givenAsShot: (Double, Double)?
    var overrides: [(String, Double)] = [], seed: UInt32 = 0
    while !arguments.isEmpty {
        let argument = arguments.removeFirst()
        func value() -> String {
            guard !arguments.isEmpty else { fail(usage) }
            return arguments.removeFirst()
        }
        func numbers(_ text: String, _ count: Int, _ separator: Character = ",") -> [Double] {
            let parts = text.split(separator: separator).compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
            guard parts.count == count else { fail("\(argument) takes \(count) numbers, as in its usage") }
            return parts
        }
        switch argument {
        case "--original": originalPath = value()
        case "--crop":
            let n = numbers(value(), 4)
            crop = CGRect(x: n[0], y: n[1], width: n[2], height: n[3])
        case "--size":
            let n = numbers(value().lowercased(), 2, "x")
            size = (Int(n[0]), Int(n[1]))
        case "--amount":
            guard let number = Double(value()), (0...2).contains(number) else { fail("--amount takes 0 to 2") }
            amount = number
        case "--as-shot":
            let n = numbers(value(), 2)
            givenAsShot = (n[0], n[1])
        case "--set":
            let pair = value().split(separator: "=", maxSplits: 1).map(String.init)
            guard pair.count == 2, CameraRawFields.paths[pair[0]] != nil, let number = Double(pair[1]) else {
                fail("--set takes field=number with a field from `comp-render preset`'s \"fields\"")
            }
            overrides.append((pair[0], number))
        case "--seed":
            guard let number = UInt32(value()) else { fail("--seed takes a whole number") }
            seed = number
        default: positional.append(argument)
        }
    }
    guard positional.count == 3 else { fail(usage) }
    let preset = loadPreset(positional[1])
    let pictureURL = URL(fileURLWithPath: positional[0])
    var picture: CGImage
    var asShot = givenAsShot
    if RawImporter.matches(pictureURL), let neutral = RawImporter.asShot(pictureURL) {
        // Decoded as shot: the preset's white balance is then reached from the camera's own reading.
        do { picture = try RawImporter.develop(pictureURL, settings: neutral) }
        catch { fail("comp-render: \(pictureURL.path): this RAW file couldn't be decoded") }
        asShot = asShot ?? (Double(neutral.temperature), Double(neutral.tint))
    } else {
        picture = loadPicture(positional[0])
    }
    if let crop {
        let rect = CGRect(x: (crop.minX * Double(picture.width)).rounded(), y: (crop.minY * Double(picture.height)).rounded(),
                          width: max(1, (crop.width * Double(picture.width)).rounded()),
                          height: max(1, (crop.height * Double(picture.height)).rounded()))
        guard let cropped = picture.cropping(to: rect.intersection(CGRect(x: 0, y: 0, width: picture.width, height: picture.height)))
        else { fail("comp-render: the crop falls outside the picture") }
        picture = cropped
    }
    if let size, (size.0, size.1) != (picture.width, picture.height) {
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(data: nil, width: size.0, height: size.1, bitsPerComponent: 8, bytesPerRow: size.0 * 4,
                                      space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { fail("comp-render: could not resize") }
        context.interpolationQuality = .high
        context.draw(picture, in: CGRect(x: 0, y: 0, width: size.0, height: size.1))
        guard let resized = context.makeImage() else { fail("comp-render: could not resize") }
        picture = resized
    }
    if let originalPath { write(picture, as: .png, to: URL(fileURLWithPath: originalPath)) }
    var mapping = PresetMapping(preset, amount: amount, asShot: asShot.map { (temperature: $0.0, tint: $0.1) })
    for (field, number) in overrides {
        if let path = CameraRawFields.paths[field] { mapping.settings[keyPath: path] = number }
    }
    let graded: CGImage
    do { graded = try mapping.settings.apply(picture, scale: 1, seed: seed) }
    catch { fail("comp-render: the grade is out of range after overrides (\(error.localizedDescription))") }
    write(graded, as: .png, to: URL(fileURLWithPath: positional[2]))
    struct Developed: Encodable {
        let preset: String, width: Int, height: Int, asShot: [Double]?, whiteBalance: PresetMapping.WhiteBalance?
        let notes: [String], skipped: [String], localCorrections: Int
    }
    printJSON(Developed(preset: preset.name, width: graded.width, height: graded.height, asShot: asShot.map { [$0.0, $0.1] },
                        whiteBalance: mapping.whiteBalance, notes: mapping.notes, skipped: mapping.skipped,
                        localCorrections: preset.corrections.filter(\.active).count))

case "subject":
    guard arguments.count == 1 else { fail(usage) }
    // Vision analyzes at sizes far below a camera's, and boxes come back as fractions, so a copy fits.
    let picture = loadPicture(arguments[0], maxSide: 1536)
    struct Box: Encodable {
        let x, y, width, height: Double
        var confidence: Double? = nil
        var coverage: Double? = nil
    }
    // Vision's rectangles are normalized with the origin at the bottom left; these count from the top left.
    func box(_ rect: CGRect, confidence: Float? = nil) -> Box {
        Box(x: rect.minX, y: 1 - rect.maxY, width: rect.width, height: rect.height, confidence: confidence.map(Double.init))
    }
    let handler = VNImageRequestHandler(cgImage: picture, orientation: .up)
    // Each runs on its own, so one that isn't available on this Mac leaves the others' answers.
    let faceRequest = VNDetectFaceRectanglesRequest()
    try? handler.perform([faceRequest])
    let peopleRequest = VNDetectHumanRectanglesRequest()
    peopleRequest.upperBodyOnly = false
    try? handler.perform([peopleRequest])
    let attentionRequest = VNGenerateAttentionBasedSaliencyImageRequest()
    try? handler.perform([attentionRequest])
    let foregroundRequest = VNGenerateForegroundInstanceMaskRequest()
    try? handler.perform([foregroundRequest])

    // The foreground's bounds, from the instance mask at the model's own resolution (rows run top to bottom).
    var subject: Box?
    if let observation = foregroundRequest.results?.first, !observation.allInstances.isEmpty,
       let mask = try? observation.generateMask(forInstances: observation.allInstances) {
        CVPixelBufferLockBaseAddress(mask, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(mask, .readOnly) }
        let width = CVPixelBufferGetWidth(mask), height = CVPixelBufferGetHeight(mask)
        let rowBytes = CVPixelBufferGetBytesPerRow(mask)
        if CVPixelBufferGetPixelFormatType(mask) == kCVPixelFormatType_OneComponent32Float,
           let base = CVPixelBufferGetBaseAddress(mask) {
            var minX = width, minY = height, maxX = -1, maxY = -1, covered = 0
            for row in 0..<height {
                let values = (base + row * rowBytes).assumingMemoryBound(to: Float.self)
                for column in 0..<width where values[column] > 0.5 {
                    minX = min(minX, column); maxX = max(maxX, column)
                    minY = min(minY, row); maxY = max(maxY, row)
                    covered += 1
                }
            }
            if maxX >= 0 {
                subject = Box(x: Double(minX) / Double(width), y: Double(minY) / Double(height),
                              width: Double(maxX - minX + 1) / Double(width), height: Double(maxY - minY + 1) / Double(height),
                              coverage: Double(covered) / Double(width * height))
            }
        }
    }
    struct Analysis: Encodable {
        let width: Int, height: Int
        let faces: [Box], people: [Box], subject: Box?, salient: [Box]
    }
    // The picture's own size, not the analyzed copy's.
    let full = CGImageSourceCreateWithURL(URL(fileURLWithPath: arguments[0]) as CFURL, nil)
        .flatMap { CGImageSourceCopyPropertiesAtIndex($0, 0, nil) as? [CFString: Any] }
    let orientation = full?[kCGImagePropertyOrientation] as? Int ?? 1
    let sides = (full?[kCGImagePropertyPixelWidth] as? Int ?? picture.width, full?[kCGImagePropertyPixelHeight] as? Int ?? picture.height)
    let (width, height) = orientation >= 5 ? (sides.1, sides.0) : sides
    printJSON(Analysis(
        width: width, height: height,
        faces: (faceRequest.results ?? []).map { box($0.boundingBox, confidence: $0.confidence) },
        people: (peopleRequest.results ?? []).map { box($0.boundingBox, confidence: $0.confidence) },
        subject: subject,
        salient: (attentionRequest.results?.first?.salientObjects ?? []).map { box($0.boundingBox, confidence: $0.confidence) }))

case "render":
    var positional: [String] = []
    var quality = 0.9, maxSide: Int?, background = (red: 1.0, green: 1.0, blue: 1.0)
    while !arguments.isEmpty {
        let argument = arguments.removeFirst()
        func value() -> String {
            guard !arguments.isEmpty else { fail(usage) }
            return arguments.removeFirst()
        }
        switch argument {
        case "--quality":
            guard let number = Double(value()) else { fail("--quality takes a number from 0 to 1") }
            quality = number
        case "--max-side":
            guard let pixels = Int(value()), pixels > 0 else { fail("--max-side takes a whole number of pixels") }
            maxSide = pixels
        case "--background":
            let hex = value().trimmingCharacters(in: CharacterSet(charactersIn: "#"))
            guard hex.count == 6, let rgb = UInt32(hex, radix: 16) else { fail("--background takes a color like FFFFFF") }
            background = (Double(rgb >> 16 & 0xFF) / 255, Double(rgb >> 8 & 0xFF) / 255, Double(rgb & 0xFF) / 255)
        default: positional.append(argument)
        }
    }
    guard positional.count == 2 else { fail(usage) }
    let outputURL = URL(fileURLWithPath: positional[1])
    let type: UTType
    switch outputURL.pathExtension.lowercased() {
    case "png": type = .png
    case "jpg", "jpeg": type = .jpeg
    default: fail("comp-render: the output must end in .png, .jpg or .jpeg")
    }
    let snapshot = await load(positional[0])
    let raster: ExportRaster
    do { raster = try await ImageExporter.shared.render(snapshot) }
    catch { fail("comp-render: \(error.localizedDescription)") }

    // Scales down (never up), and for JPEG flattens onto the background color, as the app's JPEG export does.
    let source = raster.image
    let scale = maxSide.map { min(1, Double($0) / Double(max(source.width, source.height))) } ?? 1
    let width = max(1, Int((Double(source.width) * scale).rounded())), height = max(1, Int((Double(source.height) * scale).rounded()))
    let opaque = type == .jpeg
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4, space: space,
                                  bitmapInfo: (opaque ? CGImageAlphaInfo.noneSkipLast : .premultipliedLast).rawValue) else {
        fail("comp-render: the image could not be rendered")
    }
    let bounds = CGRect(x: 0, y: 0, width: width, height: height)
    if opaque {
        context.setFillColor(red: background.red, green: background.green, blue: background.blue, alpha: 1)
        context.fill(bounds)
    }
    context.interpolationQuality = .high
    context.draw(source, in: bounds)
    guard let image = context.makeImage() else { fail("comp-render: the image could not be rendered") }
    var properties: [CFString: Any] = [kCGImagePropertyDPIWidth: raster.resolution, kCGImagePropertyDPIHeight: raster.resolution]
    if opaque { properties[kCGImageDestinationLossyCompressionQuality] = min(1, max(0, quality)) }
    write(image, as: type, to: outputURL, properties: properties)
    print("\(outputURL.path) \(width)×\(height)")

default:
    fail(usage)
}
