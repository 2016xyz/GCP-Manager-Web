<?php
/**
 * Db —— SQLite 连接与事务
 *
 * 并发模型说明（重要）：
 *   Python 版是单进程多线程，用 threading.RLock 串行化写操作。
 *   PHP-FPM 是**多进程**，进程间没有共享内存锁 —— 绝不能试图用文件锁去模拟
 *   线程锁（既不可靠也会拖垮并发）。正确做法是交给 SQLite 自己：
 *     · WAL 日志模式：读不阻塞写、写不阻塞读
 *     · busy_timeout=5000：遇到写锁竞争自动重试 5 秒，而不是立刻 SQLITE_BUSY
 *     · 写事务用 BEGIN IMMEDIATE：立刻取写锁，避免「升级锁」时的死锁窗口
 *   所有写操作必须短小（毫秒级），禁止在事务里做网络请求。
 */

declare(strict_types=1);

final class Db
{
    private static ?PDO $pdo = null;

    public static function conn(): PDO
    {
        if (self::$pdo instanceof PDO) {
            return self::$pdo;
        }
        Config::ensureDataDirs();
        $path = Config::dbPath();

        $pdo = new PDO('sqlite:' . $path, null, null, [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES   => false,
        ]);
        $pdo->exec('PRAGMA journal_mode = WAL');
        $pdo->exec('PRAGMA busy_timeout = 5000');
        $pdo->exec('PRAGMA foreign_keys = ON');
        $pdo->exec('PRAGMA synchronous = NORMAL');

        @chmod($path, 0600);
        return self::$pdo = $pdo;
    }

    /**
     * 事务包裹。写操作用 BEGIN IMMEDIATE 立刻取写锁。
     * 回调抛异常则回滚并把异常继续抛出（调用方决定如何转成 HTTP 错误）。
     */
    public static function tx(callable $fn)
    {
        $pdo = self::conn();
        $pdo->exec('BEGIN IMMEDIATE');
        try {
            $r = $fn($pdo);
            $pdo->exec('COMMIT');
            return $r;
        } catch (Throwable $e) {
            try {
                $pdo->exec('ROLLBACK');
            } catch (Throwable $ignored) {
                // 回滚失败（连接已断）时无需再处理，原始异常更重要
            }
            throw $e;
        }
    }

    /** 便捷：取单行 */
    public static function one(string $sql, array $args = []): ?array
    {
        $st = self::conn()->prepare($sql);
        $st->execute($args);
        $r = $st->fetch();
        return $r === false ? null : $r;
    }

    /** 便捷：取多行 */
    public static function all(string $sql, array $args = []): array
    {
        $st = self::conn()->prepare($sql);
        $st->execute($args);
        return $st->fetchAll();
    }

    /** 便捷：执行写，返回受影响行数 */
    public static function exec(string $sql, array $args = []): int
    {
        $st = self::conn()->prepare($sql);
        $st->execute($args);
        return $st->rowCount();
    }

    /** 便捷：取首列标量 */
    public static function scalar(string $sql, array $args = [])
    {
        $st = self::conn()->prepare($sql);
        $st->execute($args);
        return $st->fetchColumn();
    }
}
