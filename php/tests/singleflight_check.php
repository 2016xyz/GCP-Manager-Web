<?php
/**
 * 单飞（single-flight）验证
 *
 * 判别信号（关键）：
 *   被测调用返回的 probe 值，若等于「持锁进程写进缓存的那个值」，
 *   说明它**等了对方算完并复用了结果** → 单飞生效。
 *   若它返回的是自己算出来的东西（对伪造 key 而言是报错），
 *   说明它压根没等、自己又打了一遍 GCP → 单飞没生效（修复前的行为）。
 *
 * 时序设计：
 *   持锁进程：抢锁 → 睡 2000ms（模拟正在打 GCP）→ 写缓存(probe=HOLDER) → 放锁
 *   被测调用：缓存为空 → 抢锁失败 → 轮询 → 读到 HOLDER 的条目 → 直接返回
 *
 * ⚠ 全程不发任何真实 GCP 请求（缓存命中即返回；即使未命中，key_path 也是
 *   伪造的，只会得到本地错误）。
 */
declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

$sections = ['summary'];
$keyPath  = '/tmp/sf_fake_key.json';
$proj     = 'sf-proj';

$dataDir   = Config::dataDir();
@mkdir($dataDir, 0700, true);
$cacheFile = $dataDir . '/inspect_cache.json';
$lockFile  = $dataDir . '/inspect_cache.lock';

$ck = md5($keyPath . '|' . $proj . '|' . implode(',', $sections) . '|' . '' . '|' . '');

// 从干净状态开始
@unlink($cacheFile);
@unlink($lockFile);
echo "═══ 初始状态 ═══\n";
echo "  cache_key   = $ck\n";
echo "  缓存文件    不存在（必然未命中）\n";
echo "  锁文件      不存在\n\n";

// ── 持锁进程：持锁 2 秒后写入「自己算出来的」缓存 ──────────────────────
$holder = <<<'PHP'
<?php
$lockFile  = getenv('SF_LOCK');
$cacheFile = getenv('SF_CACHE');
$ck        = getenv('SF_CK');

$fh = fopen($lockFile, 'c');
if ($fh === false || !flock($fh, LOCK_EX)) { fwrite(STDERR, "holder: 抢锁失败\n"); exit(2); }
fwrite(STDOUT, "holder: 已持锁，开始「计算」（模拟打 GCP 2000ms）\n");
fflush(STDOUT);

sleep(2);   // 模拟几十次 GCP API 调用

// 把「算好的」结果写进缓存（走真实写入路径，含 LOCK_EX）
$all = json_decode((string) @file_get_contents($cacheFile), true);
if (!is_array($all)) { $all = []; }
$all[$ck] = [
    'summary' => ['ok' => true, 'ms' => 1, 'data' => ['probe' => 'HOLDER']],
    '_ts' => time(),
];
file_put_contents($cacheFile, json_encode($all), LOCK_EX);
@chmod($cacheFile, 0600);
fwrite(STDOUT, "holder: 已写入缓存(probe=HOLDER)，即将放锁\n");
fflush(STDOUT);

flock($fh, LOCK_UN);
fclose($fh);
PHP;
file_put_contents('/tmp/sf_holder.php', $holder);

$descr = [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
$proc = proc_open(
    [PHP_BINARY, '/tmp/sf_holder.php'],
    $descr,
    $pipes,
    null,
    array_merge($_ENV, ['SF_LOCK' => $lockFile, 'SF_CACHE' => $cacheFile, 'SF_CK' => $ck])
);
if (!is_resource($proc)) { echo "❌ 起不了 holder\n"; exit(1); }
stream_set_blocking($pipes[1], false);

// 等 holder 拿稳锁
$t0 = microtime(true); $ready = false;
while (microtime(true) - $t0 < 5) {
    $line = fgets($pipes[1]);
    if ($line !== false && strpos($line, '已持锁') !== false) { $ready = true; break; }
    usleep(50000);
}
if (!$ready) { echo "❌ holder 未就绪，测试无效\n"; proc_terminate($proc, 9); exit(1); }
echo "═══ 持锁进程已就绪（锁已被占用）═══\n\n";

// ── 被测调用 ─────────────────────────────────────────────────────────
echo "═══ 被测：Inspect::inspect_sections()（锁被别人占着、缓存为空）═══\n";
$t = microtime(true);
$res = Inspect::inspect_sections($keyPath, $proj, 'x@y.z', $sections, [], '', 'HTTPS', 1, false, true);
$ms = (int) ((microtime(true) - $t) * 1000);

$probe  = $res['summary']['data']['probe'] ?? null;
$cached = $res['cached'] ?? null;

printf("  耗时      %d ms\n", $ms);
printf("  cached    %s\n", var_export($cached, true));
printf("  probe     %s\n", var_export($probe, true));

echo "\n═══ 判定 ═══\n";
$ok = true;
if ($probe === 'HOLDER') {
    echo "  ✅ probe=HOLDER —— 复用了持锁进程算出来的结果，自己没有重复计算\n";
} else {
    echo "  ❌ probe=" . var_export($probe, true)
       . " —— 不是持锁进程写的值，说明它没等、自己又算了一遍（单飞失效）\n";
    $ok = false;
}
if ($cached === true) {
    echo "  ✅ 响应带 cached=true\n";
} else {
    echo "  ❌ cached 标记不是 true\n";
    $ok = false;
}
if ($ms >= 1500) {
    echo "  ✅ 耗时 {$ms}ms —— 确实等待了持锁进程（它睡了 2000ms）\n";
} else {
    echo "  ❌ 耗时只有 {$ms}ms —— 没等，直接自己算了\n";
    $ok = false;
}
echo $ok ? "\n  ★ 单飞（single-flight）生效：并发请求不会重复打 GCP\n"
         : "\n  ★ 单飞未生效\n";

@unlink($cacheFile); @unlink($lockFile); @unlink('/tmp/sf_holder.php');
proc_terminate($proc, 9);
exit($ok ? 0 : 1);
