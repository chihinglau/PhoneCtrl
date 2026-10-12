// PhoneCtrl — minimal zero-dependency PNG decoder (8-bit RGB / RGBA).
//
// Android `screencap -p` emits 8-bit RGBA PNGs (color type 6). We support
// color types 2 (RGB) and 6 (RGBA) with all five unfilter modes, enough for
// ad/reward detection. No third-party packages (uses dart:io ZLibCodec).

import 'dart:io';
import 'dart:typed_data';

import 'models.dart';

class PngImage {
  PngImage(this.width, this.height, this.rgba); // RGBA, row-major
  final int width;
  final int height;
  final Uint8List rgba;

  /// Average RGB over a relative rect [x,y,w,h].
  (int, int, int) avgRgb(Rect r) {
    final x0 = (r.x * width).round().clamp(0, width - 1);
    final y0 = (r.y * height).round().clamp(0, height - 1);
    final x1 = ((r.x + r.w) * width).round().clamp(x0 + 1, width);
    final y1 = ((r.y + r.h) * height).round().clamp(y0 + 1, height);
    int sr = 0, sg = 0, sb = 0, n = 0;
    for (int y = y0; y < y1; y += 2) {
      for (int x = x0; x < x1; x += 2) {
        final i = (y * width + x) * 4;
        sr += rgba[i];
        sg += rgba[i + 1];
        sb += rgba[i + 2];
        n++;
      }
    }
    if (n == 0) return (0, 0, 0);
    return (sr ~/ n, sg ~/ n, sb ~/ n);
  }

  /// Crop a relative rect into a new PngImage.
  PngImage crop(Rect r) {
    final x0 = (r.x * width).round().clamp(0, width);
    final y0 = (r.y * height).round().clamp(0, height);
    final cw = ((r.x + r.w) * width).round() - x0;
    final ch = ((r.y + r.h) * height).round() - y0;
    final out = Uint8List(cw * ch * 4);
    for (int y = 0; y < ch; y++) {
      for (int x = 0; x < cw; x++) {
        final si = ((y0 + y) * width + (x0 + x)) * 4;
        final di = (y * cw + x) * 4;
        out[di] = rgba[si];
        out[di + 1] = rgba[si + 1];
        out[di + 2] = rgba[si + 2];
        out[di + 3] = rgba[si + 3];
      }
    }
    return PngImage(cw, ch, out);
  }

  /// Mean absolute difference vs another (same-size) image, 0..1.
  double mad(PngImage o) {
    if (o.width != width || o.height != height) return 1.0;
    int d = 0;
    final n = rgba.length ~/ 4;
    for (int i = 0; i < rgba.length; i += 4) {
      d += (rgba[i] - o.rgba[i]).abs() +
          (rgba[i + 1] - o.rgba[i + 1]).abs() +
          (rgba[i + 2] - o.rgba[i + 2]).abs();
    }
    return (d / n) / (3 * 255);
  }
}

PngImage decodePng(Uint8List bytes) {
  if (bytes.length < 8 ||
      bytes[0] != 137 ||
      bytes[1] != 80 ||
      bytes[2] != 78 ||
      bytes[3] != 71) {
    throw Exception('not a PNG');
  }
  int pos = 8;
  int? width, height, bitDepth, colorType;
  final idat = <int>[];
  while (pos < bytes.length) {
    final len = _rd32(bytes, pos);
    final type = String.fromCharCodes(bytes.sublist(pos + 4, pos + 8));
    final data = bytes.sublist(pos + 8, pos + 8 + len);
    if (type == 'IHDR') {
      width = _rd32(data, 0);
      height = _rd32(data, 4);
      bitDepth = data[8];
      colorType = data[9];
    } else if (type == 'IDAT') {
      idat.addAll(data);
    } else if (type == 'IEND') {
      break;
    }
    pos += 12 + len;
  }
  if (width == null || height == null) throw Exception('PNG missing IHDR');
  if (bitDepth != 8 || (colorType != 2 && colorType != 6)) {
    throw Exception('unsupported PNG color type $colorType / depth $bitDepth');
  }
  final channels = colorType == 6 ? 4 : 3;
  final raw = _inflate(Uint8List.fromList(idat));
  final stride = width * channels;
  final out = Uint8List(width * height * 4);
  final prev = Uint8List(stride);
  int rp = 0;
  for (int y = 0; y < height; y++) {
    final filter = raw[rp++];
    final line = Uint8List(stride);
    for (int x = 0; x < stride; x++) {
      final rawByte = raw[rp++];
      final a = x >= channels ? line[x - channels] : 0;
      final b = prev[x];
      final c = x >= channels ? prev[x - channels] : 0;
      int v;
      switch (filter) {
        case 0: v = rawByte; break;
        case 1: v = rawByte + a; break;
        case 2: v = rawByte + b; break;
        case 3: v = rawByte + ((a + b) ~/ 2); break;
        case 4:
          final p = a + b - c;
          final pa = (p - a).abs(), pb = (p - b).abs(), pc = (p - c).abs();
          v = rawByte + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c);
          break;
        default: v = rawByte;
      }
      line[x] = v & 0xff;
    }
    for (int x = 0; x < width; x++) {
      final di = (y * width + x) * 4;
      final si = x * channels;
      out[di] = line[si];
      out[di + 1] = line[si + 1];
      out[di + 2] = line[si + 2];
      out[di + 3] = channels == 6 ? line[si + 3] : 255;
    }
    line.setRange(0, stride, prev); // prev = line (copy)
    for (int i = 0; i < stride; i++) prev[i] = line[i];
  }
  return PngImage(width, height, out);
}

int _rd32(Uint8List b, int o) =>
    (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3];

List<int> _inflate(Uint8List zlibData) {
  // PNG IDAT is a zlib stream; dart:io ZLibCodec decodes zlib (header+adler).
  final codec = ZLibCodec();
  return codec.decode(zlibData);
}
