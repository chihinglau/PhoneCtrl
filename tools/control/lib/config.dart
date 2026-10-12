// PhoneCtrl — JSON task script (data-driven, reusable across apps).
//
// Mirrors the reference project's "config separated from code" philosophy
// (GamePouch library/index.json). See docs §6.1 for the schema.

import 'dart:convert';

import 'models.dart';

class TaskScript {
  TaskScript({
    required this.appName,
    required this.package,
    this.activity,
    this.home,
    required this.loop,
  });

  factory TaskScript.fromJson(Map<String, dynamic> j) => TaskScript(
        appName: j['appName'] ?? 'unknown',
        package: j['package'] ?? '',
        activity: j['activity'],
        home: j['home'] != null ? _pt(j['home']['tap']) : null,
        loop: LoopConfig.fromJson(j['loop'] ?? {}),
      );

  factory TaskScript.parse(String text) => TaskScript.fromJson(jsonDecode(text));

  final String appName;
  final String package;
  final String? activity;
  final RPoint? home;
  final LoopConfig loop;

  Map<String, dynamic> toJson() => {
        'appName': appName,
        'package': package,
        if (activity != null) 'activity': activity,
        if (home != null) 'home': {'tap': [home!.x, home!.y]},
        'loop': loop.toJson(),
      };
}

class LoopConfig {
  LoopConfig({
    this.rounds = 10,
    this.openApp = true,
    this.swipe,
    this.ad,
    this.reward,
    this.nav,
    this.videoAd,
    this.backHome = true,
    this.intervalMs = 1000,
  });

  factory LoopConfig.fromJson(Map<String, dynamic> j) => LoopConfig(
        rounds: j['rounds'] ?? 10,
        openApp: j['openApp'] ?? true,
        swipe: j['swipe'] != null ? SwipeConfig.fromJson(j['swipe']) : null,
        ad: j['ad'] != null ? AdConfig.fromJson(j['ad']) : null,
        reward: j['reward'] != null ? RewardConfig.fromJson(j['reward']) : null,
        nav: (j['nav'] as List?)
            ?.map((e) => NavStep.fromJson(e as Map<String, dynamic>))
            .toList(),
        videoAd: j['videoAd'] != null
            ? VideoAdConfig.fromJson(j['videoAd'] as Map<String, dynamic>)
            : null,
        backHome: j['backHome'] ?? true,
        intervalMs: j['intervalMs'] ?? 1000,
      );

  final int rounds;
  final bool openApp;
  final SwipeConfig? swipe;
  final AdConfig? ad;
  final RewardConfig? reward;
  final List<NavStep>? nav; // 到达任务入口前的若干次导航点击（App 相关）
  final VideoAdConfig? videoAd; // 看视频广告 -> 等待 -> 领取 -> 校验
  final bool backHome;
  final int intervalMs;

  Map<String, dynamic> toJson() => {
        'rounds': rounds,
        'openApp': openApp,
        if (swipe != null) 'swipe': swipe!.toJson(),
        if (ad != null) 'ad': ad!.toJson(),
        if (reward != null) 'reward': reward!.toJson(),
        if (nav != null) 'nav': nav!.map((e) => e.toJson()).toList(),
        if (videoAd != null) 'videoAd': videoAd!.toJson(),
        'backHome': backHome,
        'intervalMs': intervalMs,
      };
}

/// 到达任务入口前的导航点击（如：点「我的」-> 点「金币任务」）。
class NavStep {
  NavStep(this.tap, this.delayMs);
  factory NavStep.fromJson(Map<String, dynamic> j) =>
      NavStep(_pt(j['tap']), j['delayMs'] ?? 800);
  final RPoint tap;
  final int delayMs;
  Map<String, dynamic> toJson() => {'tap': [tap.x, tap.y], 'delayMs': delayMs};
}

/// 看视频广告 -> 等待视频播完 -> 领取 -> 校验。
/// 入口/领取均可「固定坐标」或「区域 + OCR 关键词」两种命中方式；
/// 即使未安装 tesseract，只要坐标/区域校准正确仍可运行（OCR 仅作可选增稳）。
class VideoAdConfig {
  VideoAdConfig({
    this.entryButton,
    this.entryRegion,
    this.entryKeywords = const [],
    this.waitClaimMs = 40000,
    this.claimButton,
    this.claimRegion,
    this.claimKeywords = const [],
    this.verifyRegion,
    this.successKeywords = const ['领取成功', '已领取', '开心收下', '+', '金币'],
    this.timeoutMs = 12000,
  });
  factory VideoAdConfig.fromJson(Map<String, dynamic> j) => VideoAdConfig(
        entryButton: j['entryButton'] != null ? _pt(j['entryButton']) : null,
        entryRegion: j['entryRegion'] != null ? _rect(j['entryRegion']) : null,
        entryKeywords: (j['entryKeywords'] as List?)
                ?.map((e) => '$e')
                .toList() ??
            const [],
        waitClaimMs: j['waitClaimMs'] ?? 40000,
        claimButton: j['claimButton'] != null ? _pt(j['claimButton']) : null,
        claimRegion: j['claimRegion'] != null ? _rect(j['claimRegion']) : null,
        claimKeywords: (j['claimKeywords'] as List?)
                ?.map((e) => '$e')
                .toList() ??
            const [],
        verifyRegion:
            j['verifyRegion'] != null ? _rect(j['verifyRegion']) : null,
        successKeywords: (j['successKeywords'] as List?)
                ?.map((e) => '$e')
                .toList() ??
            const ['领取成功', '已领取', '开心收下', '+', '金币'],
        timeoutMs: j['timeoutMs'] ?? 12000,
      );
  final RPoint? entryButton; // 点击「看视频得金币」入口（固定坐标）
  final Rect? entryRegion; // 入口区域（点中心；若有 entryKeywords 则 OCR 命中才点）
  final List<String> entryKeywords;
  final int waitClaimMs; // 轮询「领取」出现的超时（视频时长 + 余量）
  final RPoint? claimButton; // 「领取」按钮固定坐标
  final Rect? claimRegion; // 「领取」按钮区域
  final List<String> claimKeywords;
  final Rect? verifyRegion; // 金币/提示校验区域
  final List<String> successKeywords;
  final int timeoutMs;
  Map<String, dynamic> toJson() => {
        if (entryButton != null)
          'entryButton': [entryButton!.x, entryButton!.y],
        if (entryRegion != null)
          'entryRegion': [
            entryRegion!.x,
            entryRegion!.y,
            entryRegion!.w,
            entryRegion!.h
          ],
        'entryKeywords': entryKeywords,
        'waitClaimMs': waitClaimMs,
        if (claimButton != null)
          'claimButton': [claimButton!.x, claimButton!.y],
        if (claimRegion != null)
          'claimRegion': [
            claimRegion!.x,
            claimRegion!.y,
            claimRegion!.w,
            claimRegion!.h
          ],
        'claimKeywords': claimKeywords,
        if (verifyRegion != null)
          'verifyRegion': [
            verifyRegion!.x,
            verifyRegion!.y,
            verifyRegion!.w,
            verifyRegion!.h
          ],
        'successKeywords': successKeywords,
        'timeoutMs': timeoutMs,
      };
}

class SwipeConfig {
  SwipeConfig(this.from, this.to, this.ms, this.times);
  factory SwipeConfig.fromJson(Map<String, dynamic> j) => SwipeConfig(
        _pt(j['from']), _pt(j['to']), j['ms'] ?? 400, j['times'] ?? 3);
  final RPoint from;
  final RPoint to;
  final int ms;
  final int times;
  Map<String, dynamic> toJson() =>
      {'from': [from.x, from.y], 'to': [to.x, to.y], 'ms': ms, 'times': times};
}

class AdConfig {
  AdConfig({
    this.region,
    this.mode = 'region',
    this.search,
    this.keywords = const [],
    this.maxClicks = 2,
  });
  factory AdConfig.fromJson(Map<String, dynamic> j) => AdConfig(
        region: j['region'] != null ? _rect(j['region']) : null,
        mode: j['mode'] ?? 'region',
        search: j['search'] != null ? _rect(j['search']) : null,
        keywords: (j['keywords'] as List?)?.map((e) => '$e').toList() ?? const [],
        maxClicks: j['maxClicks'] ?? 2,
      );
  final Rect? region;
  final String mode; // 'region' | 'template' | 'ocr'
  final Rect? search;
  final List<String> keywords;
  final int maxClicks;
  Map<String, dynamic> toJson() => {
        if (region != null) 'region': [region!.x, region!.y, region!.w, region!.h],
        'mode': mode,
        if (search != null) 'search': [search!.x, search!.y, search!.w, search!.h],
        'keywords': keywords,
        'maxClicks': maxClicks,
      };
}

class RewardConfig {
  RewardConfig(this.button, this.verifyRegion, this.timeoutMs, this.successKeywords);
  factory RewardConfig.fromJson(Map<String, dynamic> j) => RewardConfig(
        _pt(j['button']),
        _rect(j['verifyRegion']),
        j['timeoutMs'] ?? 8000,
        (j['successKeywords'] as List?)?.map((e) => '$e').toList() ?? const ['领取成功', '已领取'],
      );
  final RPoint button;
  final Rect verifyRegion;
  final int timeoutMs;
  final List<String> successKeywords;
  Map<String, dynamic> toJson() => {
        'button': [button.x, button.y],
        'verifyRegion': [verifyRegion.x, verifyRegion.y, verifyRegion.w, verifyRegion.h],
        'timeoutMs': timeoutMs,
        'successKeywords': successKeywords,
      };
}

RPoint _pt(dynamic v) => RPoint((v[0] as num).toDouble(), (v[1] as num).toDouble());
Rect _rect(dynamic v) => Rect((v[0] as num).toDouble(), (v[1] as num).toDouble(),
    (v[2] as num).toDouble(), (v[3] as num).toDouble());
