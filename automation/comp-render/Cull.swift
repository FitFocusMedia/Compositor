import Foundation
import CoreGraphics
import ImageIO
import Vision

// What a shoot's frames are, for `comp-render probe`, and how good each one is, for `comp-render score`: read from the
// camera's embedded preview (a RAW is never decoded), so thousands of frames take minutes, not hours.

/// A frame's metadata, without decoding it.
nonisolated struct FrameInfo: Encodable, Sendable {
    let path: String
    var width = 0, height = 0, orientation = 1
    /// Capture time as "yyyy-MM-dd HH:mm:ss.SSS", from DateTimeOriginal and its sub-seconds.
    var captured: String?
    var iso: Double?, shutter: Double?, aperture: Double?, focalLength: Double?
    var camera: String?
    /// The star rating written in camera or in Lightroom (XMP), when there is one.
    var rating: Int?

    init(url: URL) {
        path = url.path
        guard let source = CGImageSourceCreateWithURL(url as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] else { return }
        orientation = properties[kCGImagePropertyOrientation] as? Int ?? 1
        let w = properties[kCGImagePropertyPixelWidth] as? Int ?? 0, h = properties[kCGImagePropertyPixelHeight] as? Int ?? 0
        (width, height) = orientation >= 5 ? (h, w) : (w, h)
        let exif = properties[kCGImagePropertyExifDictionary] as? [CFString: Any] ?? [:]
        if let date = exif[kCGImagePropertyExifDateTimeOriginal] as? String {
            let subsec = (exif[kCGImagePropertyExifSubsecTimeOriginal] as? String).map { String($0.prefix(3)) } ?? "000"
            let parts = date.split(separator: " ")
            if parts.count == 2 {
                captured = parts[0].replacingOccurrences(of: ":", with: "-") + " " + parts[1] + "." + subsec.padding(toLength: 3, withPad: "0", startingAt: 0)
            }
        }
        iso = (exif[kCGImagePropertyExifISOSpeedRatings] as? [Double])?.first
        shutter = exif[kCGImagePropertyExifExposureTime] as? Double
        aperture = exif[kCGImagePropertyExifFNumber] as? Double
        focalLength = exif[kCGImagePropertyExifFocalLength] as? Double
        let tiff = properties[kCGImagePropertyTIFFDictionary] as? [CFString: Any] ?? [:]
        camera = [tiff[kCGImagePropertyTIFFMake] as? String, tiff[kCGImagePropertyTIFFModel] as? String].compactMap { $0 }.joined(separator: " ")
        if let metadata = CGImageSourceCopyMetadataAtIndex(source, 0, nil),
           let tag = CGImageMetadataCopyTagWithPath(metadata, nil, "xmp:Rating" as CFString),
           let value = CGImageMetadataTagCopyValue(tag) as? String {
            rating = Int(value)
        }
        // A Lightroom sidecar beside the RAW holds the rating once it's been rated there.
        let sidecar = url.deletingPathExtension().appendingPathExtension("xmp")
        if let text = try? String(contentsOf: sidecar, encoding: .utf8),
           let range = text.range(of: #"xmp:Rating="(-?\d+)""#, options: .regularExpression) {
            rating = Int(text[range].filter { $0.isNumber || $0 == "-" })
        }
    }
}

/// How good a frame looks, measured on its embedded preview.
nonisolated struct FrameScore: Encodable, Sendable {
    struct Face: Encodable, Sendable {
        let box: [Double]
        /// Vision's face capture quality, 0–1: sharpness, lighting, pose and expression of the face as a portrait.
        var quality: Double?
        /// Eye openness, left and right: height over width of each eye's outline (about 0.25+ open, under 0.12 shut).
        var eyes: [Double]?
        /// The face's sharpness, as `sharpness` below.
        var sharpness: Double?
    }
    let path: String
    var preview: [Int] = []
    /// Vision's aesthetic score, −1…1, and whether it judged the picture a utility shot (screenshot, document).
    var aesthetics: Double?
    var utility: Bool?
    var faces: [Face] = []
    /// How crisp the frame's sharpest subject is (any face big enough to judge, or a salient region; else the whole
    /// frame), as fine detail over tonal range. Comparable within a shoot, not across cameras.
    var sharpness: Double?
    var frameSharpness: Double?
    /// The sharpest spot anywhere in the frame (best of a 6 × 6 grid of tiles with real detail): low only when
    /// nothing is in focus, as with a missed focus or motion blur, not when a shallow focus is deliberate.
    var peakSharpness: Double?
    var subject: [Double]?
    /// Mean lightness (L*, 0–100) of the preview and the share of blown and crushed pixels.
    var lightness: Double?
    var blown: Double?, crushed: Double?
    /// Distance between this frame's and the previous frame's Vision feature prints (0 identical; under ~0.5 the
    /// same moment), when frames were given in capture order.
    var distanceToPrevious: Double?
    var error: String?
}

nonisolated enum Culling {
    static func preview(_ url: URL, longSide: Int) -> CGImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary) else { return nil }
        return CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageIfAbsent: true, kCGImageSourceThumbnailMaxPixelSize: longSide,
            kCGImageSourceCreateThumbnailWithTransform: true, kCGImageSourceShouldCacheImmediately: true,
        ] as CFDictionary)
    }

    /// Grayscale bytes of `image`, at its own size.
    static func gray(_ image: CGImage) -> (pixels: [UInt8], width: Int, height: Int)? {
        let width = image.width, height = image.height
        var pixels = [UInt8](repeating: 0, count: width * height)
        guard let context = CGContext(data: &pixels, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width,
                                      space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return (pixels, width, height)
    }

    /// How crisp `box` is (fractions from the top-left): the 95th percentile of |Laplacian| over the region's own tonal
    /// range (5th to 95th percentile), so a dark or low-contrast subject in focus isn't mistaken for a soft one.
    static func sharpness(_ gray: (pixels: [UInt8], width: Int, height: Int), box: CGRect, minimumRange: Double = 0) -> Double? {
        let x0 = max(1, Int(box.minX * Double(gray.width))), x1 = min(gray.width - 1, Int(box.maxX * Double(gray.width)))
        let y0 = max(1, Int(box.minY * Double(gray.height))), y1 = min(gray.height - 1, Int(box.maxY * Double(gray.height)))
        guard x1 - x0 > 8, y1 - y0 > 8 else { return nil }
        var histogram = [Int](repeating: 0, count: 1021), levels = [Int](repeating: 0, count: 256)
        var count = 0
        gray.pixels.withUnsafeBufferPointer { p in
            let w = gray.width
            for y in y0..<y1 {
                for x in x0..<x1 {
                    let i = y * w + x
                    let laplacian = 4 * Int(p[i]) - Int(p[i - 1]) - Int(p[i + 1]) - Int(p[i - w]) - Int(p[i + w])
                    histogram[min(1020, abs(laplacian))] += 1
                    levels[Int(p[i])] += 1
                    count += 1
                }
            }
        }
        func percentile(_ counts: [Int], _ share: Double) -> Double {
            var seen = 0
            for (value, n) in counts.enumerated() {
                seen += n
                if Double(seen) >= share * Double(count) { return Double(value) }
            }
            return Double(counts.count - 1)
        }
        let range = percentile(levels, 0.95) - percentile(levels, 0.05)
        guard range >= minimumRange else { return nil }
        return 100 * percentile(histogram, 0.95) / (range + 10)
    }

    static func eyeOpenness(_ region: VNFaceLandmarkRegion2D?) -> Double? {
        guard let points = region?.normalizedPoints, points.count >= 4 else { return nil }
        let xs = points.map(\.x), ys = points.map(\.y)
        let width = (xs.max() ?? 0) - (xs.min() ?? 0)
        guard width > 0 else { return nil }
        return Double(((ys.max() ?? 0) - (ys.min() ?? 0)) / width)
    }

    /// Vision's rectangles are normalized from the bottom-left; scores report them from the top-left.
    static func box(_ rect: CGRect) -> CGRect { CGRect(x: rect.minX, y: 1 - rect.maxY, width: rect.width, height: rect.height) }

    static func score(_ url: URL, longSide: Int) -> (FrameScore, VNFeaturePrintObservation?) {
        var result = FrameScore(path: url.path)
        guard let image = preview(url, longSide: longSide) else {
            result.error = "no preview"
            return (result, nil)
        }
        result.preview = [image.width, image.height]
        let handler = VNImageRequestHandler(cgImage: image, orientation: .up)
        let aesthetics = VNCalculateImageAestheticsScoresRequest()
        let quality = VNDetectFaceCaptureQualityRequest()
        let landmarks = VNDetectFaceLandmarksRequest()
        let attention = VNGenerateAttentionBasedSaliencyImageRequest()
        let fingerprint = VNGenerateImageFeaturePrintRequest()
        for request in [aesthetics, quality, landmarks, attention, fingerprint] as [VNRequest] { try? handler.perform([request]) }
        if let observation = aesthetics.results?.first {
            result.aesthetics = Double(observation.overallScore)
            result.utility = observation.isUtility
        }
        let gray = gray(image)
        let qualities = quality.results ?? []
        for face in landmarks.results ?? [] {
            let rect = box(face.boundingBox)
            var entry = FrameScore.Face(box: [rect.minX, rect.minY, rect.width, rect.height])
            // The same face from the quality request: the one whose box overlaps most.
            entry.quality = qualities.max { a, b in
                a.boundingBox.intersection(face.boundingBox).area < b.boundingBox.intersection(face.boundingBox).area
            }.flatMap { $0.boundingBox.intersects(face.boundingBox) ? $0.faceCaptureQuality.map(Double.init) : nil }
            if let left = eyeOpenness(face.landmarks?.leftEye), let right = eyeOpenness(face.landmarks?.rightEye) { entry.eyes = [left, right] }
            if let gray { entry.sharpness = sharpness(gray, box: rect.insetBy(dx: -rect.width * 0.15, dy: -rect.height * 0.15)) }
            result.faces.append(entry)
        }
        // The face in focus leads, not the biggest: a blurred foreground face is often framing, not the subject.
        // Faces too small to judge (under 0.2% of the frame) go last.
        result.faces.sort { a, b in
            let aBig = a.box[2] * a.box[3] >= 0.002, bBig = b.box[2] * b.box[3] >= 0.002
            return aBig != bBig ? aBig : (a.sharpness ?? 0) > (b.sharpness ?? 0)
        }
        let salientBoxes = attention.results?.first?.salientObjects?.map { box($0.boundingBox) } ?? []
        let salient = salientBoxes.dropFirst().reduce(salientBoxes.first) { union, next in union?.union(next) }
        if let salient { result.subject = [salient.minX, salient.minY, salient.width, salient.height] }
        if let gray {
            result.frameSharpness = sharpness(gray, box: CGRect(x: 0, y: 0, width: 1, height: 1))
            // A frame is as sharp as its sharpest subject: any face big enough to judge, or anything Vision found salient.
            let candidates = result.faces.filter { $0.box[2] * $0.box[3] >= 0.002 }.compactMap(\.sharpness)
                + salientBoxes.compactMap { sharpness(gray, box: $0) }
            result.sharpness = candidates.max() ?? result.frameSharpness
            var peak: Double?
            for row in 0..<6 {
                for column in 0..<6 {
                    let tile = CGRect(x: Double(column) / 6, y: Double(row) / 6, width: 1.0 / 6, height: 1.0 / 6)
                    if let value = sharpness(gray, box: tile, minimumRange: 24) { peak = max(peak ?? 0, value) }
                }
            }
            result.peakSharpness = peak
            var sum = 0.0, blown = 0, crushed = 0
            for value in gray.pixels {
                let v = Double(value) / 255
                let linear = v <= 0.04045 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4)
                sum += linear > 216.0 / 24389 ? 116 * cbrt(linear) - 16 : 24389.0 / 27 * linear
                if value >= 254 { blown += 1 }
                if value <= 1 { crushed += 1 }
            }
            let n = Double(gray.pixels.count)
            result.lightness = sum / n
            result.blown = Double(blown) / n * 100
            result.crushed = Double(crushed) / n * 100
        }
        return (result, fingerprint.results?.first)
    }
}

nonisolated extension CGRect {
    var area: CGFloat { isNull ? 0 : width * height }
}
