#!/usr/bin/env php
<?php
/**
 * proxy-check.php —— 代理配置诊断工具（CLI）
 *
 * 用法：
 *     php bin/proxy-check.php 'socks5h://user:pass@host:1080'
 *     php bin/proxy-check.php '1.2.3.4:8080' HTTPS
 *     php bin/proxy-check.php ''                       # 传空串 = 测直连
 *
 * 它做的事就是后台「测试代理」按钮做的事：解析 → 经该代理发一个真实请求到
 * PROXY_TEST_URL，打印解析结果、归一化类型、状态码与延迟。
 * 排查「明明代理能用、面板却说不可用」时先用它，能把问题定位到
 * 「解析层」还是「连通层」，不用去点网页。
 *
 * 注意：本文件在 bin/ 下，nginx 已对 /bin/ 返回 404，不会被 Web 访问到。
 */
require __DIR__ . '/../src/Gcp.php';

$proxy = $argv[1] ?? '';
$type  = strtoupper($argv[2] ?? 'SOCKS5H');

echo "支持的类型（Gcp::PROXY_TYPE_LABELS）：" . implode(' / ', array_keys(Gcp::PROXY_TYPE_LABELS)) . "\n";
echo "入参：proxy=" . ($proxy === '' ? '(空→直连)' : Gcp::mask_proxy($proxy)) . "  proxy_type={$type}\n";
echo str_repeat('-', 72) . "\n";

$p = Gcp::parse_proxy_input($proxy, $type);
echo "解析：ok=" . var_export($p['ok'], true)
    . "  empty=" . var_export($p['empty'] ?? null, true)
    . "  归一化类型=" . ($p['proxy_type'] ?? '-')
    . "  标签=" . ($p['proxy_type_label'] ?? '-') . "\n";
if (!($p['ok'] ?? false)) {
    echo "❌ 解析失败：" . ($p['error'] ?? '') . "\n";
    echo "   → 问题在**解析层**（写法/协议名不对），还没到网络\n";
    exit(1);
}
echo "  地址：" . ($p['proxy_url'] ?? '(直连)') . "\n";

$t0 = microtime(true);
$res = Gcp::test_proxy($proxy, $type);
$ms = (int) round((microtime(true) - $t0) * 1000);

echo "连通：ok=" . var_export($res['ok'] ?? null, true)
    . "  status=" . ($res['status'] ?? '-')
    . "  latency=" . ($res['latency_ms'] ?? $ms) . "ms\n";
if (!empty($res['error'])) {
    echo "❌ 错误：" . $res['error'] . "\n";
    echo "   → 问题在**连通层**";
    echo !empty($res['blocked_by_proxy']) ? "（代理活着但拦了这个目标）\n" : "（代理不可达 / 认证失败 / 目标被拒）\n";
    exit(1);
}
echo "✅ 代理可用：经 {$res['via']} 到达 {$res['url']}，HTTP {$res['status']}\n";
exit(0);
