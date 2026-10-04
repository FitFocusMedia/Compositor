import AppKit
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

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
      prints {"width", "height", "padding", "baseline"} as JSON: the PNG's size and, for placing point text, how far
      the first baseline sits below the image's top edge and the text's left edge from its left edge
  comp-render defaults
      prints the app's default adjustment, effect and text records and its blend mode names as JSON
  comp-render cutout <picture> <output.png> [--mask] [--refine 0-40] [--contrast 0-100] [--shift -10-10] [--basic]
      Remove Background: the picture with its background transparent, or with --mask the grayscale subject mask
      (white over the subject) to use as a layer mask. Refines edges as the app's Advanced mode (refine 12,
      contrast 25, shift 0); --basic takes Vision's mask as it comes
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
    struct Placement: Encodable { let width: Int, height: Int, padding: Double, baseline: Double }
    printJSON(Placement(width: image.width, height: image.height, padding: LayerTextStyle.padding,
                        baseline: LayerTextStyle.padding + style.lineHeight - descent))

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
    let inputURL = URL(fileURLWithPath: positional[0])
    // Upright (EXIF orientation applied) and in sRGB, as an imported picture is.
    guard let source = CGImageSourceCreateWithURL(inputURL as CFURL, nil),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let pixelWidth = properties[kCGImagePropertyPixelWidth] as? Int, let pixelHeight = properties[kCGImagePropertyPixelHeight] as? Int,
          let oriented = CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: max(pixelWidth, pixelHeight), kCGImageSourceShouldCacheImmediately: true
          ] as CFDictionary),
          let space = CGColorSpace(name: CGColorSpace.sRGB),
          let canvas = CGContext(data: nil, width: oriented.width, height: oriented.height, bitsPerComponent: 8,
                                 bytesPerRow: oriented.width * 4, space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        fail("comp-render: \(inputURL.path): not a picture macOS can read")
    }
    let bounds = CGRect(x: 0, y: 0, width: oriented.width, height: oriented.height)
    canvas.draw(oriented, in: bounds)
    guard let picture = canvas.makeImage() else { fail("comp-render: could not read the picture") }
    let mask: CGImage
    do { mask = try SubjectRemoval.subjectMask(picture, under: nil, settings: settings) }
    catch { fail("comp-render: \(error.localizedDescription)") }
    let outputURL = URL(fileURLWithPath: positional[1])
    if maskOnly {
        write(mask, as: .png, to: outputURL)
    } else {
        canvas.clear(bounds)
        canvas.saveGState()
        canvas.clip(to: bounds, mask: mask)
        canvas.draw(picture, in: bounds)
        canvas.restoreGState()
        guard let cutout = canvas.makeImage() else { fail("comp-render: could not render the cutout") }
        write(cutout, as: .png, to: outputURL)
    }
    print("\(outputURL.path) \(mask.width)×\(mask.height)")

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
