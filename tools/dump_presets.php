<?php
// 把 5 个预设的脚本原文导出，逐个查 OS 假设
require dirname(__DIR__) . '/php/src/Catalog.php';
require dirname(__DIR__) . '/php/src/InstallPresets.php';
$all = InstallPresets::presets();
foreach (InstallPresets::PRESET_ORDER as $k) {
    $s = $all[$k]['script'] ?? '';
    $lines = explode("\n", $s);
    $hits = [];
    foreach ($lines as $i => $l) {
        if (preg_match('/apt(-get)?|dnf|yum|rpm\b|deb\.nodesource|rpm\.nodesource|os-release|ID_LIKE|apk\b|zypper|pacman/', $l)) {
            $hits[] = sprintf("%3d| %s", $i + 1, trim($l));
        }
    }
    printf("── %-8s (%d 行) ──\n", $k, count($lines));
    if ($hits) {
        foreach ($hits as $h) { echo "  ", mb_substr($h, 0, 150), "\n"; }
    } else {
        echo "  (无包管理器相关行 —— 用的是通用安装脚本)\n";
    }
    echo "\n";
}
