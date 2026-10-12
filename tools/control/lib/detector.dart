// PhoneCtrl — ad recognition + reward verification strategies (strategy pattern).
//
// Three pluggable detectors compose for the same frame: hit ANY one and we
// click. Reward verification uses a before/after snapshot to avoid "fake claim".

import 'dart:io';
import 'dart:typed_data';

import 'models.dart';
import 'png.dart';

/// A detected click target in relative coordinates.
class Detection {
  Detection(this.click, this.tag);
  final RPoint click;
  final String tag;
}

/// OCR plug-in: defaults to a local `tesseract` shell-out; returns '' if absent.
abstract class OcrProvider {
  Future<String> text(PngImage img);
}

class TesseractOcrProvider implements OcrProvider {
  TesseractOcrProvider({this.lang = 'chi_sim+eng'});
  final String lang;
  @override
  Future<String> text(PngImage img) async {
    final tmp = File('${Directory.systemTemp.path}/pc_ocr_${img.width}x${img.height}.png');
    // minimal PNG encode (RGBA) for tesseract input
    await tmp.writeAsBytes(_encodePng(img), flush: true);
    try {
      final r = await Process.run('tesseract', [tmp.path, 'stdout', '-l', lang],
          stdoutEncoding: systemEncoding);
      return r.exitCode == 0 ? (r.stdout as String) : '';
    } catch (_) {
      return '';
    } finally {
      try { tmp.deleteSync(); } catch (_) {}
    }
  }
}

/// Base interface for detectors that turn a frame into a click target.
abstract class AdDetector {
  Future<Detection?> detect(PngImage frame);
}

/// 1) Region / coordinate strategy (most robust, zero CV).
///    mode 'always' -> always tap the region center (fixed layout apps).
///    mode 'color'  -> tap only when region avg color matches a signature.
class RegionAdDetector implements AdDetector {
  RegionAdDetector(this.region, {this.mode = 'always', this.expectedColors, this.tol = 40});
  final Rect region;
  final String mode;
  final List<(int, int, int)>? expectedColors;
  final int tol;

  @override
  Future<Detection?> detect(PngImage frame) async {
    final (cx, cy) = region.center;
    if (mode == 'color') {
      final (r, g, b) = frame.avgRgb(region);
      final hit = expectedColors?.any((c) =>
          (c.$1 - r).abs() + (c.$2 - g).abs() + (c.$3 - b).abs() < tol * 3);
      if (hit != true) return null;
    }
    return Detection(RPoint(cx, cy), 'region:$mode');
  }
}

/// 2) Template-matching strategy (NCC via mean-absolute-difference).
class TemplateAdDetector implements AdDetector {
  TemplateAdDetector(this.template, this.search, {this.threshold = 0.12});
  final PngImage template;
  final Rect search;
  final double threshold; // lower = stricter

  @override
  Future<Detection?> detect(PngImage frame) async {
    final hay = frame.crop(search);
    // slide template over hay at 1/20 steps
    final stepX = (hay.width / 20).round().clamp(1, hay.width);
    final stepY = (hay.height / 20).round().clamp(1, hay.height);
    double best = 1.0;
    int bx = 0, by = 0;
    for (int y = 0; y + template.height <= hay.height; y += stepY) {
      for (int x = 0; x + template.width <= hay.width; x += stepX) {
        final sub = hay.crop(Rect(x / hay.width, y / hay.height,
            template.width / hay.width, template.height / hay.height));
        final d = sub.mad(template);
        if (d < best) { best = d; bx = x; by = y; }
      }
    }
    if (best > threshold) return null;
    final rx = (search.x + (bx + template.width / 2) / hay.width * search.w);
    final ry = (search.y + (by + template.height / 2) / hay.height * search.h);
    return Detection(RPoint(rx, ry), 'template(mad=${best.toStringAsFixed(3)})');
  }
}

/// 3) OCR keyword strategy.
class OcrAdDetector implements AdDetector {
  OcrAdDetector(this.ocr, this.keywords, this.region);
  final OcrProvider ocr;
  final List<String> keywords;
  final Rect region;

  @override
  Future<Detection?> detect(PngImage frame) async {
    final text = await ocr.text(frame.crop(region));
    if (keywords.any((k) => text.contains(k))) {
      final (cx, cy) = region.center;
      return Detection(RPoint(cx, cy), 'ocr:$text');
    }
    return null;
  }
}

/// Reward verification (three-tier, see docs §4.2).
class RewardVerifier {
  RewardVerifier(this.ocr);
  final OcrProvider ocr;

  /// Returns true if a reward was credibly collected between [before] and [after].
  Future<bool> verify(PngImage before, PngImage after, Rect verifyRegion,
      {List<String> successKeywords = const ['领取成功', '已领取', '+', '金币', '成功']}) async {
    // tier 2: success keyword appears after but not before
    final tb = await ocr.text(before.crop(verifyRegion));
    final ta = await ocr.text(after.crop(verifyRegion));
    if (successKeywords.any((k) => ta.contains(k)) &&
        !successKeywords.any((k) => tb.contains(k))) {
      return true;
    }
    // tier 1: numeric value increased (best-effort OCR parse)
    final vb = _firstInt(ta), va = _firstInt(tb);
    if (vb != null && va != null && va > vb) return true;
    return false;
  }

  int? _firstInt(String s) {
    final m = RegExp(r'(\d+)').firstMatch(s.replaceAll(',', ''));
    return m != null ? int.parse(m.group(1)!) : null;
  }
}

/// Minimal PNG encoder (RGBA) used to feed external OCR/templating tools.
Uint8List _encodePng(PngImage img) {
  final w = img.width, h = img.height;
  final raw = <int>[];
  const stride = 4;
  for (int y = 0; y < h; y++) {
    raw.add(0); // filter type 0 (None) per scanline
    for (int x = 0; x < w; x++) {
      final i = (y * w + x) * stride;
      raw.add(img.rgba[i]);
      raw.add(img.rgba[i + 1]);
      raw.add(img.rgba[i + 2]);
      raw.add(img.rgba[i + 3]);
    }
  }
  final deflated = ZLibCodec().encode(Uint8List.fromList(raw));
  final out = <int>[]
    ..addAll([137, 80, 78, 71, 13, 10, 26, 10]);
  void chunk(String type, List<int> data) {
    out.addAll(_u32(data.length));
    out.addAll(type.codeUnits);
    out.addAll(data);
    out.addAll(_u32(_crc(type, data)));
  }
  chunk('IHDR', [
    ..._u32(w), ..._u32(h), 8, 6, 0, 0, 0, // 8-bit RGBA
  ]);
  chunk('IDAT', deflated);
  chunk('IEND', []);
  return Uint8List.fromList(out);
}

List<int> _u32(int v) => [(v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255];

int _crc(String type, List<int> data) {
  final buf = <int>[...type.codeUnits, ...data];
  var c = 0xffffffff;
  for (final b in buf) {
    c ^= b;
    for (int k = 0; k < 8; k++) {
      c = (c & 1) != 0 ? (c >> 1) ^ 0xedb88320 : c >> 1;
    }
  }
  return (c ^ 0xffffffff) & 0xffffffff;
}
