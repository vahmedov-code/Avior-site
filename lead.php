<?php
// ============================================================
// AVIOR — приёмник заявок с сайта → уведомление в MAX
// Положить рядом с index.html. Секреты — в config.php (не в этом файле).
// ============================================================

header('Content-Type: application/json; charset=utf-8');

// CORS: разрешаем запросы только со своего домена
$allowed = ['https://avior.moscow', 'https://www.avior.moscow'];
$origin  = $_SERVER['HTTP_ORIGIN'] ?? '';
if (in_array($origin, $allowed, true)) {
    header("Access-Control-Allow-Origin: $origin");
}
header('Access-Control-Allow-Headers: Content-Type');
header('Access-Control-Allow-Methods: POST, OPTIONS');

if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') { http_response_code(204); exit; }
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    http_response_code(405); echo json_encode(['ok' => false]); exit;
}

$cfg = require __DIR__ . '/config.php';

// --- читаем и чистим данные ---
$raw  = file_get_contents('php://input');
$data = json_decode($raw, true) ?: [];

$clean = static function ($v, $max = 500) {
    $v = is_string($v) ? $v : '';
    $v = strip_tags(trim($v));
    $v = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F]/u', '', $v);
    return mb_substr($v, 0, $max);
};

$name    = $clean($data['name']    ?? '', 100);
$phone   = $clean($data['phone']   ?? '', 50);
$channel = $clean($data['channel'] ?? '', 20);
$text    = $clean($data['text']    ?? '', 1500);
$page    = $clean($data['page']    ?? '', 200);

// honeypot: если скрытое поле заполнено — это бот, тихо отвечаем ОК
if (!empty($data['website'])) { echo json_encode(['ok' => true]); exit; }

if ($name === '' || $phone === '') {
    http_response_code(400); echo json_encode(['ok' => false, 'error' => 'empty']); exit;
}

// --- простая защита от флуда: не чаще 1 заявки в 20 сек с одного IP ---
$ip   = $_SERVER['REMOTE_ADDR'] ?? '0.0.0.0';
$lock = sys_get_temp_dir() . '/avior_lead_' . md5($ip);
if (file_exists($lock) && (time() - filemtime($lock)) < 20) {
    http_response_code(429); echo json_encode(['ok' => false, 'error' => 'too_fast']); exit;
}
touch($lock);

// --- формируем сообщение ---
$channels = ['telegram' => 'Telegram', 'max' => 'MAX', 'call' => 'Звонок'];
$chLabel  = $channels[$channel] ?? $channel;

$msg  = "🔧 Новая заявка с сайта\n\n";
$msg .= "Имя: {$name}\n";
$msg .= "Контакт: {$phone}\n";
if ($chLabel !== '') { $msg .= "Ответить в: {$chLabel}\n"; }
if ($text !== '')    { $msg .= "\nПроблема:\n{$text}\n"; }
$msg .= "\n" . date('d.m.Y H:i');

// --- отправка в MAX ---
$ok = false;
$url = 'https://platform-api.max.ru/messages?chat_id=' . urlencode($cfg['max_chat_id']);
$ch  = curl_init($url);
curl_setopt_array($ch, [
    CURLOPT_POST           => true,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT        => 10,
    CURLOPT_HTTPHEADER     => [
        'Authorization: ' . $cfg['max_token'],
        'Content-Type: application/json',
    ],
    CURLOPT_POSTFIELDS     => json_encode(['text' => $msg], JSON_UNESCAPED_UNICODE),
]);
$resp = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err  = curl_error($ch);
curl_close($ch);

$ok = ($code >= 200 && $code < 300);

// --- лог (пригодится при отладке; чистить раз в пару месяцев) ---
$line = date('c') . " | ip={$ip} | http={$code} | " . str_replace("\n", ' / ', $msg)
      . ($ok ? '' : " | ERR: {$err} {$resp}") . "\n";
@file_put_contents(__DIR__ . '/leads.log', $line, FILE_APPEND | LOCK_EX);

// --- ответ сайту ---
if ($ok) {
    echo json_encode(['ok' => true]);
} else {
    http_response_code(502);
    echo json_encode(['ok' => false, 'error' => 'send_failed']);
}
