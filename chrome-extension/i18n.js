// Minimal runtime i18n (no Chrome _locales dependency)
(() => {
  const SUPPORTED_LANGS = ['en', 'zh-TW', 'zh-CN', 'ja', 'ko', 'fr', 'es', 'pt'];
  const DEFAULT_LANG = 'en';

  function normalizeLang(input) {
    if (!input || typeof input !== 'string') return '';
    const raw = input.trim().toLowerCase();
    if (!raw) return '';

    // Keep backward compatibility: previous versions stored "zh" which mapped to Traditional.
    if (raw === 'zh') return 'zh-TW';

    // Chinese variants
    if (raw === 'zh-tw' || raw.startsWith('zh-tw') || raw.includes('zh-hant') || raw.includes('hant') || raw.startsWith('zh-hk') || raw.startsWith('zh-mo')) return 'zh-TW';
    if (raw === 'zh-cn' || raw.startsWith('zh-cn') || raw.includes('zh-hans') || raw.includes('hans') || raw.startsWith('zh-sg')) return 'zh-CN';
    if (raw.startsWith('zh')) return 'zh-CN';

    if (raw.startsWith('ja')) return 'ja';
    if (raw.startsWith('ko')) return 'ko';
    if (raw.startsWith('fr')) return 'fr';
    if (raw.startsWith('es')) return 'es';
    if (raw.startsWith('pt')) return 'pt';
    if (raw.startsWith('en')) return 'en';
    return '';
  }

  function detectDefaultLanguage() {
    const nav = normalizeLang(navigator.language || navigator.userLanguage || '');
    return nav && SUPPORTED_LANGS.includes(nav) ? nav : DEFAULT_LANG;
  }

  const MESSAGES = {
    en: {
      'status.checking': 'Checking...',
      'status.notConfigured': 'Not configured',
      'status.connected': 'Connected',
      'status.disconnected': 'Disconnected',

      'btn.refresh.title': 'Refresh',
      'btn.settings.title': 'Settings',
      'section.detectedVideos': 'Detected Videos',
      'section.recentDownloads': 'Recent Downloads on NAS',
      'empty.noVideos.title': '🔍 No videos detected yet',
      'empty.noVideos.hint': 'Browse to a video streaming site',
      'empty.noJobs.title': '📥 No recent downloads on NAS',
      'empty.noJobs.short': 'No recent downloads',

      'url.ipWarning.title': 'IP-Restricted URL Detected',
      'url.ipWarning.body': 'This URL contains an IP address, meaning the website restricts downloads to that specific IP.\nTo download successfully, your NAS and PC must use the same IP address.\nUse Tailscale exit node or a similar VPN solution to route the traffic through the same IP address.',
      'url.nowPlaying': 'Now Playing',
      'url.sendToNas': 'Send to NAS',
      'url.copy': 'Copy',

      'job.duration': 'Duration: {duration}',
      'job.cancel.title': 'Cancel download',
      'job.cancel': 'Cancel',
      'job.solution': 'Suggested Solution',

      'alert.configureFirst': 'Please configure NAS settings first',
      'video.untitled': 'Untitled Video',
      'toast.sending': 'Sending to NAS...',
      'toast.failedToSend': 'Failed to send',
      'toast.nasNotConfigured': '❌ NAS not configured',
      'toast.jobCancelled': 'Job cancelled',
      'toast.failedToCancel': 'Failed to cancel job',
      'toast.copied': 'Copied to clipboard',

      'jobStatus.pending': 'Pending',
      'jobStatus.downloading': 'Downloading',
      'jobStatus.processing': 'Processing',
      'jobStatus.completed': 'Completed',
      'jobStatus.failed': 'Failed',
      'jobStatus.cancelled': 'Cancelled',

      'error.unknown.type': 'Unknown Error',
      'error.unknown.message': 'No error details available',
      'error.unknown.solution': 'Try again or check the NAS logs for more information.',

      'error.403.type': 'Access Denied (403)',
      'error.403.solution': 'This website likely uses <strong>IP-based authentication</strong>. The video URL was generated for your PC\'s IP address, but your NAS has a different IP.\n<ul>\n  <li>Use <strong>Tailscale Exit Node</strong> to route NAS traffic through your PC</li>\n  <li>Run the downloader on your local PC instead of NAS</li>\n  <li>Use a VPN to give both devices the same public IP</li>\n</ul>',

      'error.404.type': 'Not Found (404)',
      'error.404.solution': 'The video URL is no longer valid.\n<ul>\n  <li>The URL has expired</li>\n  <li>The video was removed</li>\n  <li>The link is temporary and needs to be refreshed</li>\n</ul>\nTry refreshing the video page and sending a new download request.',

      'error.timeout.type': 'Connection Timeout',
      'error.timeout.solution': 'The connection to the video server timed out.\n<ul>\n  <li>Check your NAS network connection</li>\n  <li>The video server might be slow or overloaded</li>\n  <li>Try again later</li>\n</ul>',

      'error.ssl.type': 'SSL/TLS Error',
      'error.ssl.solution': 'There was a problem with the secure connection.\n<ul>\n  <li>Check if your NAS system time is correct</li>\n  <li>The website might have an invalid certificate</li>\n  <li>Try updating the downloader to the latest version</li>\n</ul>',

      'error.connection.type': 'Connection Error',
      'error.connection.solution': 'Could not connect to the video server.\n<ul>\n  <li>Check your NAS internet connection</li>\n  <li>The video server might be down</li>\n  <li>Check if your NAS can access external websites</li>\n</ul>',

      'error.invalidPlaylist.type': 'Invalid Playlist',
      'error.invalidPlaylist.solution': 'The m3u8 playlist is empty or invalid.\n<ul>\n  <li>The video requires authentication</li>\n  <li>The playlist URL is incomplete</li>\n  <li>The video format is not supported</li>\n</ul>',

      'error.generic.type': 'Download Failed',
      'error.generic.solution': 'An error occurred during download.\n<ul>\n  <li>Check NAS logs for more details</li>\n  <li>Try refreshing the video page and resending</li>\n  <li>Some websites have download protection that cannot be bypassed</li>\n</ul>',

      'options.pageTitle': 'WebVideo2NAS Settings',
      'options.subtitle': 'Configure your NAS connection',
      'options.nasConfig.title': 'NAS Configuration',
      'options.nasEndpoint.label': 'NAS Endpoint URL',
      'options.nasEndpoint.helpHtml': 'Format: <code>http://YOUR_NAS_IP:PORT</code><br>Example: <code>http://192.168.50.181:52052</code>',
      'options.apiKey.label': 'API Key',
      'options.apiKey.helpHtml': 'Find this in your NAS <code>.env</code> file',
      'options.btn.test': 'Test Connection',
      'options.btn.save': 'Save Settings',
      'options.downloadPreferences.title': 'Download Preferences',
      'options.uiLanguage.label': 'Side Panel language',
      'options.uiLanguage.auto': 'Auto (browser language)',
      'options.uiLanguage.help': 'Applies to the Side Panel UI.',
      'options.autoDetect.label': 'Auto-detect video URLs',
      'options.autoDetect.help': 'Automatically detect M3U8 and MP4 URLs when browsing',
      'options.showNotifications.label': 'Show notifications',
      'options.showNotifications.help': 'Show notifications when download is submitted',
      'options.about.title': 'About',
      'options.about.version': 'Version:',
      'options.about.author': 'Author:',
      'options.about.authorValue': 'WebVideo2NAS Team',
      'options.about.description': 'This extension allows you to send web videos (M3U8 streams, MP4 files) directly to your NAS for download.',
      'options.howToUse.title': '📖 How to use:',
      'options.howToUse.step1': 'Configure your NAS endpoint and API key above',
      'options.howToUse.step2': 'Browse to a video streaming website',
      'options.howToUse.step3': 'Video URLs (M3U8, MP4) will be detected automatically',
      'options.howToUse.step4': 'Click the extension icon to see detected URLs',
      'options.howToUse.step5': 'Click "Send to NAS" to start downloading',
      'options.needHelp.title': '🔧 Need help?',
      'options.needHelp.bodyHtml': '• Check your NAS is accessible from this computer<br>• Verify API port is correct (default: 52052; maps to API container :8000)<br>• Ensure API key matches your NAS configuration<br>• Check firewall settings on your NAS',
      'options.footer': 'WebVideo2NAS © 2025',

      'options.status.enterNasEndpoint': 'Please enter NAS endpoint URL',
      'options.status.enterApiKey': 'Please enter API key',
      'options.status.invalidUrl': 'Invalid URL format. Use http:// or https://',
      'options.status.saved': '✅ Settings saved successfully!',
      'options.status.enterBoth': 'Please enter both NAS endpoint and API key',
      'options.status.testing': '🔍 Testing connection...',
      'options.status.connectionOk': '✅ Connection successful! Your NAS is reachable.',
      'options.status.connectedWithStats': '✅ Connected! Active downloads: {active}, Queue: {queue}',
      'options.status.unexpectedResponse': 'Unexpected response from NAS',
      'options.status.connectionFailedPrefix': '❌ Connection failed: ',
      'options.status.cannotReach': 'Cannot reach NAS. Check IP address and port.',
      'options.status.invalidApiKey': 'Invalid API key.',
      'options.status.apiNotFound': 'API endpoint not found. Check NAS configuration.'
    },
    'zh-TW': {
      'status.checking': '檢查中…',
      'status.notConfigured': '尚未設定',
      'status.connected': '已連線',
      'status.disconnected': '未連線',

      'btn.refresh.title': '重新整理',
      'btn.settings.title': '設定',
      'section.detectedVideos': '偵測到的影片',
      'section.recentDownloads': 'NAS 近期下載',
      'empty.noVideos.title': '🔍 尚未偵測到影片',
      'empty.noVideos.hint': '請前往影片串流網站瀏覽',
      'empty.noJobs.title': '📥 NAS 沒有近期下載',
      'empty.noJobs.short': '沒有近期下載',

      'url.ipWarning.title': '偵測到 IP 限制的 URL',
      'url.ipWarning.body': '此 URL 含有 IP 位址，代表網站可能限制只能由該 IP 下載。\n要下載成功，NAS 與電腦必須使用相同的 IP 位址。\n可使用 Tailscale Exit Node 或類似 VPN，讓流量走同一個 IP。',
      'url.nowPlaying': '正在播放',
      'url.sendToNas': '送到 NAS',
      'url.copy': '複製',

      'job.duration': '片長：{duration}',
      'job.cancel.title': '取消下載',
      'job.cancel': '取消',
      'job.solution': '建議解法',

      'alert.configureFirst': '請先完成 NAS 設定',
      'video.untitled': '未命名影片',
      'toast.sending': '送出到 NAS…',
      'toast.failedToSend': '送出失敗',
      'toast.nasNotConfigured': '❌ 尚未設定 NAS',
      'toast.jobCancelled': '已取消工作',
      'toast.failedToCancel': '取消失敗',
      'toast.copied': '已複製到剪貼簿',

      'jobStatus.pending': '等待中',
      'jobStatus.downloading': '下載中',
      'jobStatus.processing': '處理中',
      'jobStatus.completed': '已完成',
      'jobStatus.failed': '失敗',
      'jobStatus.cancelled': '已取消',

      'error.unknown.type': '未知錯誤',
      'error.unknown.message': '沒有可用的錯誤細節',
      'error.unknown.solution': '請重試，或到 NAS 日誌查看更多資訊。',

      'error.403.type': '拒絕存取 (403)',
      'error.403.solution': '此網站可能使用 <strong>IP 驗證</strong>。影片 URL 是為你電腦的 IP 產生的，但 NAS 的 IP 不同。\n<ul>\n  <li>使用 <strong>Tailscale Exit Node</strong> 讓 NAS 流量經由你的電腦</li>\n  <li>改在本機電腦上執行 downloader，而不是 NAS</li>\n  <li>使用 VPN 讓兩台設備有相同的對外 IP</li>\n</ul>',

      'error.404.type': '找不到 (404)',
      'error.404.solution': '影片 URL 已失效。\n<ul>\n  <li>URL 已過期</li>\n  <li>影片已移除</li>\n  <li>連結是暫時性的，需要重新取得</li>\n</ul>\n請重新整理影片頁面後再送出下載。',

      'error.timeout.type': '連線逾時',
      'error.timeout.solution': '連線到影片伺服器逾時。\n<ul>\n  <li>檢查 NAS 網路連線</li>\n  <li>影片伺服器可能過慢或過載</li>\n  <li>稍後再試</li>\n</ul>',

      'error.ssl.type': 'SSL/TLS 錯誤',
      'error.ssl.solution': '安全連線發生問題。\n<ul>\n  <li>確認 NAS 系統時間是否正確</li>\n  <li>網站可能使用無效憑證</li>\n  <li>嘗試更新 downloader 到最新版</li>\n</ul>',

      'error.connection.type': '連線錯誤',
      'error.connection.solution': '無法連線到影片伺服器。\n<ul>\n  <li>檢查 NAS 網際網路連線</li>\n  <li>影片伺服器可能暫時故障</li>\n  <li>確認 NAS 能否連到外部網站</li>\n</ul>',

      'error.invalidPlaylist.type': '播放清單無效',
      'error.invalidPlaylist.solution': 'm3u8 播放清單為空或無效。\n<ul>\n  <li>影片需要驗證/登入</li>\n  <li>播放清單 URL 不完整</li>\n  <li>影片格式不支援</li>\n</ul>',

      'error.generic.type': '下載失敗',
      'error.generic.solution': '下載過程發生錯誤。\n<ul>\n  <li>到 NAS 日誌查看更多細節</li>\n  <li>重新整理影片頁面後再送出</li>\n  <li>部分網站有下載保護，可能無法繞過</li>\n</ul>',

      'options.pageTitle': 'WebVideo2NAS 設定',
      'options.subtitle': '設定你的 NAS 連線',
      'options.nasConfig.title': 'NAS 設定',
      'options.nasEndpoint.label': 'NAS 端點 URL',
      'options.nasEndpoint.helpHtml': '格式：<code>http://YOUR_NAS_IP:PORT</code><br>範例：<code>http://192.168.50.181:52052</code>',
      'options.apiKey.label': 'API 金鑰',
      'options.apiKey.helpHtml': '可在 NAS 的 <code>.env</code> 檔案中找到',
      'options.btn.test': '測試連線',
      'options.btn.save': '儲存設定',
      'options.downloadPreferences.title': '下載偏好',
      'options.uiLanguage.label': 'Side Panel 語言',
      'options.uiLanguage.auto': '自動（跟隨瀏覽器語言）',
      'options.uiLanguage.help': '套用於 Side Panel 介面。',
      'options.autoDetect.label': '自動偵測影片 URL',
      'options.autoDetect.help': '瀏覽時自動偵測 M3U8 與 MP4 URL',
      'options.showNotifications.label': '顯示通知',
      'options.showNotifications.help': '送出下載時顯示通知',
      'options.about.title': '關於',
      'options.about.version': '版本：',
      'options.about.author': '作者：',
      'options.about.authorValue': 'WebVideo2NAS 團隊',
      'options.about.description': '此擴充套件可將網頁影片（M3U8 串流、MP4 檔案）送到你的 NAS 下載。',
      'options.howToUse.title': '📖 使用方式：',
      'options.howToUse.step1': '在上方設定 NAS 端點與 API 金鑰',
      'options.howToUse.step2': '前往影片串流網站瀏覽',
      'options.howToUse.step3': '系統會自動偵測影片 URL（M3U8、MP4）',
      'options.howToUse.step4': '點擊擴充套件圖示查看偵測到的 URL',
      'options.howToUse.step5': '點擊「送到 NAS」開始下載',
      'options.needHelp.title': '🔧 需要協助？',
      'options.needHelp.bodyHtml': '• 確認此電腦可連到 NAS<br>• 確認 API 埠號正確（預設：52052；對應到 API 容器 :8000）<br>• 確認 API 金鑰與 NAS 設定一致<br>• 檢查 NAS 防火牆設定',
      'options.footer': 'WebVideo2NAS © 2025',

      'options.status.enterNasEndpoint': '請輸入 NAS 端點 URL',
      'options.status.enterApiKey': '請輸入 API 金鑰',
      'options.status.invalidUrl': 'URL 格式不正確，請使用 http:// 或 https://',
      'options.status.saved': '✅ 設定已儲存！',
      'options.status.enterBoth': '請輸入 NAS 端點與 API 金鑰',
      'options.status.testing': '🔍 測試連線中…',
      'options.status.connectionOk': '✅ 連線成功！NAS 可正常連線。',
      'options.status.connectedWithStats': '✅ 已連線！進行中：{active}，佇列：{queue}',
      'options.status.unexpectedResponse': 'NAS 回應非預期',
      'options.status.connectionFailedPrefix': '❌ 連線失敗：',
      'options.status.cannotReach': '無法連到 NAS，請檢查 IP 與埠號。',
      'options.status.invalidApiKey': 'API 金鑰無效。',
      'options.status.apiNotFound': '找不到 API 端點，請檢查 NAS 設定。'
    },
    'zh-CN': {
      'status.checking': '检查中…',
      'status.notConfigured': '尚未设置',
      'status.connected': '已连接',
      'status.disconnected': '未连接',

      'btn.refresh.title': '刷新',
      'btn.settings.title': '设置',
      'section.detectedVideos': '检测到的视频',
      'section.recentDownloads': 'NAS 最近下载',
      'empty.noVideos.title': '🔍 尚未检测到视频',
      'empty.noVideos.hint': '请前往视频流媒体网站浏览',
      'empty.noJobs.title': '📥 NAS 没有最近下载',
      'empty.noJobs.short': '没有最近下载',

      'url.ipWarning.title': '检测到 IP 限制的 URL',
      'url.ipWarning.body': '此 URL 含有 IP 地址，表示网站可能限制只能由该 IP 下载。\n要下载成功，NAS 与电脑必须使用相同的 IP 地址。\n可使用 Tailscale Exit Node 或类似 VPN，让流量走同一个 IP。',
      'url.nowPlaying': '正在播放',
      'url.sendToNas': '发送到 NAS',
      'url.copy': '复制',

      'job.duration': '时长：{duration}',
      'job.cancel.title': '取消下载',
      'job.cancel': '取消',
      'job.solution': '建议解法',

      'alert.configureFirst': '请先完成 NAS 设置',
      'video.untitled': '未命名视频',
      'toast.sending': '正在发送到 NAS…',
      'toast.failedToSend': '发送失败',
      'toast.nasNotConfigured': '❌ 尚未设置 NAS',
      'toast.jobCancelled': '已取消任务',
      'toast.failedToCancel': '取消失败',
      'toast.copied': '已复制到剪贴板',

      'jobStatus.pending': '等待中',
      'jobStatus.downloading': '下载中',
      'jobStatus.processing': '处理中',
      'jobStatus.completed': '已完成',
      'jobStatus.failed': '失败',
      'jobStatus.cancelled': '已取消',

      'error.unknown.type': '未知错误',
      'error.unknown.message': '没有可用的错误详情',
      'error.unknown.solution': '请重试，或到 NAS 日志查看更多信息。',

      'error.403.type': '拒绝访问 (403)',
      'error.403.solution': '此网站可能使用 <strong>IP 验证</strong>。视频 URL 是为你电脑的 IP 生成的，但 NAS 的 IP 不同。\n<ul>\n  <li>使用 <strong>Tailscale Exit Node</strong> 让 NAS 流量经由你的电脑</li>\n  <li>改在本机电脑上运行 downloader，而不是 NAS</li>\n  <li>使用 VPN 让两台设备有相同的对外 IP</li>\n</ul>',

      'error.404.type': '找不到 (404)',
      'error.404.solution': '视频 URL 已失效。\n<ul>\n  <li>URL 已过期</li>\n  <li>视频已移除</li>\n  <li>链接是临时的，需要重新获取</li>\n</ul>\n请刷新视频页面后再发送下载。',

      'error.timeout.type': '连接超时',
      'error.timeout.solution': '连接到视频服务器超时。\n<ul>\n  <li>检查 NAS 网络连接</li>\n  <li>视频服务器可能过慢或过载</li>\n  <li>稍后再试</li>\n</ul>',

      'error.ssl.type': 'SSL/TLS 错误',
      'error.ssl.solution': '安全连接出现问题。\n<ul>\n  <li>确认 NAS 系统时间是否正确</li>\n  <li>网站可能使用无效证书</li>\n  <li>尝试将 downloader 更新到最新版</li>\n</ul>',

      'error.connection.type': '连接错误',
      'error.connection.solution': '无法连接到视频服务器。\n<ul>\n  <li>检查 NAS 互联网连接</li>\n  <li>视频服务器可能暂时故障</li>\n  <li>确认 NAS 能否访问外部网站</li>\n</ul>',

      'error.invalidPlaylist.type': '播放列表无效',
      'error.invalidPlaylist.solution': 'm3u8 播放列表为空或无效。\n<ul>\n  <li>视频需要验证/登录</li>\n  <li>播放列表 URL 不完整</li>\n  <li>视频格式不支持</li>\n</ul>',

      'error.generic.type': '下载失败',
      'error.generic.solution': '下载过程中发生错误。\n<ul>\n  <li>到 NAS 日志查看更多细节</li>\n  <li>刷新视频页面后再发送</li>\n  <li>部分网站有下载保护，可能无法绕过</li>\n</ul>',

      'options.pageTitle': 'WebVideo2NAS 设置',
      'options.subtitle': '配置你的 NAS 连接',
      'options.nasConfig.title': 'NAS 配置',
      'options.nasEndpoint.label': 'NAS 端点 URL',
      'options.nasEndpoint.helpHtml': '格式：<code>http://YOUR_NAS_IP:PORT</code><br>示例：<code>http://192.168.50.181:52052</code>',
      'options.apiKey.label': 'API Key',
      'options.apiKey.helpHtml': '可在 NAS 的 <code>.env</code> 文件中找到',
      'options.btn.test': '测试连接',
      'options.btn.save': '保存设置',
      'options.downloadPreferences.title': '下载偏好',
      'options.uiLanguage.label': 'Side Panel 语言',
      'options.uiLanguage.auto': '自动（跟随浏览器语言）',
      'options.uiLanguage.help': '应用于 Side Panel 界面。',
      'options.autoDetect.label': '自动检测视频 URL',
      'options.autoDetect.help': '浏览时自动检测 M3U8 与 MP4 URL',
      'options.showNotifications.label': '显示通知',
      'options.showNotifications.help': '提交下载时显示通知',
      'options.about.title': '关于',
      'options.about.version': '版本：',
      'options.about.author': '作者：',
      'options.about.authorValue': 'WebVideo2NAS 团队',
      'options.about.description': '该扩展可将网页视频（M3U8 流、MP4 文件）发送到你的 NAS 下载。',
      'options.howToUse.title': '📖 使用方法：',
      'options.howToUse.step1': '在上方配置 NAS 端点与 API Key',
      'options.howToUse.step2': '前往视频流媒体网站浏览',
      'options.howToUse.step3': '系统会自动检测视频 URL（M3U8、MP4）',
      'options.howToUse.step4': '点击扩展图标查看检测到的 URL',
      'options.howToUse.step5': '点击“发送到 NAS”开始下载',
      'options.needHelp.title': '🔧 需要帮助？',
      'options.needHelp.bodyHtml': '• 确认此电脑可访问 NAS<br>• 确认 API 端口正确（默认：52052；映射到 API 容器 :8000）<br>• 确认 API Key 与 NAS 配置一致<br>• 检查 NAS 防火墙设置',
      'options.footer': 'WebVideo2NAS © 2025',

      'options.status.enterNasEndpoint': '请输入 NAS 端点 URL',
      'options.status.enterApiKey': '请输入 API Key',
      'options.status.invalidUrl': 'URL 格式不正确，请使用 http:// 或 https://',
      'options.status.saved': '✅ 设置已保存！',
      'options.status.enterBoth': '请输入 NAS 端点与 API Key',
      'options.status.testing': '🔍 正在测试连接…',
      'options.status.connectionOk': '✅ 连接成功！NAS 可访问。',
      'options.status.connectedWithStats': '✅ 已连接！活动下载：{active}，队列：{queue}',
      'options.status.unexpectedResponse': 'NAS 响应异常',
      'options.status.connectionFailedPrefix': '❌ 连接失败：',
      'options.status.cannotReach': '无法访问 NAS，请检查 IP 和端口。',
      'options.status.invalidApiKey': 'API Key 无效。',
      'options.status.apiNotFound': '找不到 API 端点，请检查 NAS 配置。'
    },
    ja: {
      'status.checking': '確認中…',
      'status.notConfigured': '未設定',
      'status.connected': '接続済み',
      'status.disconnected': '未接続',

      'btn.refresh.title': '更新',
      'btn.settings.title': '設定',
      'section.detectedVideos': '検出された動画',
      'section.recentDownloads': 'NAS の最近のダウンロード',
      'empty.noVideos.title': '🔍 まだ動画が検出されていません',
      'empty.noVideos.hint': '動画配信サイトを開いてください',
      'empty.noJobs.title': '📥 NAS に最近のダウンロードはありません',
      'empty.noJobs.short': '最近のダウンロードはありません',

      'url.ipWarning.title': 'IP 制限の URL を検出',
      'url.ipWarning.body': 'この URL には IP アドレスが含まれており、サイトが特定の IP のみでダウンロードを許可している可能性があります。\n成功させるには、NAS と PC が同じ IP を使用する必要があります。\nTailscale の Exit Node などの VPN で同じ IP 経由にしてください。',
      'url.nowPlaying': '再生中',
      'url.sendToNas': 'NAS に送信',
      'url.copy': 'コピー',

      'job.duration': '所要時間: {duration}',
      'job.cancel.title': 'ダウンロードをキャンセル',
      'job.cancel': 'キャンセル',
      'job.solution': '推奨対処',

      'alert.configureFirst': '先に NAS 設定を行ってください',
      'video.untitled': '無題の動画',
      'toast.sending': 'NAS に送信中…',
      'toast.failedToSend': '送信に失敗しました',
      'toast.nasNotConfigured': '❌ NAS 未設定',
      'toast.jobCancelled': 'キャンセルしました',
      'toast.failedToCancel': 'キャンセルに失敗しました',
      'toast.copied': 'クリップボードにコピーしました',

      'jobStatus.pending': '待機中',
      'jobStatus.downloading': 'ダウンロード中',
      'jobStatus.processing': '処理中',
      'jobStatus.completed': '完了',
      'jobStatus.failed': '失敗',
      'jobStatus.cancelled': 'キャンセル済み',

      'error.unknown.type': '不明なエラー',
      'error.unknown.message': 'エラー詳細がありません',
      'error.unknown.solution': '再試行するか、NAS のログを確認してください。',

      'error.403.type': 'アクセス拒否 (403)',
      'error.403.solution': 'このサイトは <strong>IP 認証</strong> を使用している可能性があります。URL は PC の IP 用に生成されており、NAS の IP が異なります。\n<ul>\n  <li><strong>Tailscale Exit Node</strong> で NAS の通信を PC 経由にする</li>\n  <li>NAS ではなくローカル PC で downloader を実行する</li>\n  <li>VPN で両端末の外部 IP を同一にする</li>\n</ul>',

      'error.404.type': '見つかりません (404)',
      'error.404.solution': '動画 URL が無効です。\n<ul>\n  <li>URL の期限切れ</li>\n  <li>動画が削除された</li>\n  <li>一時的なリンクで再取得が必要</li>\n</ul>\n動画ページを更新して再送信してください。',

      'error.timeout.type': 'タイムアウト',
      'error.timeout.solution': '動画サーバーへの接続がタイムアウトしました。\n<ul>\n  <li>NAS のネットワークを確認</li>\n  <li>サーバーが遅い/混雑している可能性</li>\n  <li>後でもう一度試す</li>\n</ul>',

      'error.ssl.type': 'SSL/TLS エラー',
      'error.ssl.solution': '安全な接続で問題が発生しました。\n<ul>\n  <li>NAS の時刻設定を確認</li>\n  <li>サイトの証明書が無効な可能性</li>\n  <li>downloader を最新に更新</li>\n</ul>',

      'error.connection.type': '接続エラー',
      'error.connection.solution': '動画サーバーに接続できません。\n<ul>\n  <li>NAS のインターネット接続を確認</li>\n  <li>サーバーがダウンしている可能性</li>\n  <li>NAS が外部サイトにアクセスできるか確認</li>\n</ul>',

      'error.invalidPlaylist.type': '無効なプレイリスト',
      'error.invalidPlaylist.solution': 'm3u8 プレイリストが空、または無効です。\n<ul>\n  <li>認証が必要</li>\n  <li>URL が不完全</li>\n  <li>形式が未対応</li>\n</ul>',

      'error.generic.type': 'ダウンロード失敗',
      'error.generic.solution': 'ダウンロード中にエラーが発生しました。\n<ul>\n  <li>NAS のログを確認</li>\n  <li>動画ページを更新して再送信</li>\n  <li>サイトの保護で回避できない場合があります</li>\n</ul>',

      'options.pageTitle': 'WebVideo2NAS 設定',
      'options.subtitle': 'NAS 接続を設定します',
      'options.nasConfig.title': 'NAS 設定',
      'options.nasEndpoint.label': 'NAS エンドポイント URL',
      'options.nasEndpoint.helpHtml': '形式: <code>http://YOUR_NAS_IP:PORT</code><br>例: <code>http://192.168.50.181:52052</code>',
      'options.apiKey.label': 'API キー',
      'options.apiKey.helpHtml': 'NAS の <code>.env</code> ファイルで確認できます',
      'options.btn.test': '接続テスト',
      'options.btn.save': '設定を保存',
      'options.downloadPreferences.title': 'ダウンロード設定',
      'options.uiLanguage.label': 'サイドパネルの言語',
      'options.uiLanguage.auto': '自動（ブラウザ言語）',
      'options.uiLanguage.help': 'サイドパネル UI に適用されます。',
      'options.autoDetect.label': '動画 URL を自動検出',
      'options.autoDetect.help': '閲覧中に M3U8 / MP4 URL を自動検出します',
      'options.showNotifications.label': '通知を表示',
      'options.showNotifications.help': 'ダウンロード送信時に通知を表示します',
      'options.about.title': '概要',
      'options.about.version': 'バージョン:',
      'options.about.author': '作成者:',
      'options.about.authorValue': 'WebVideo2NAS Team',
      'options.about.description': 'この拡張機能は、Web 動画（M3U8 ストリーム、MP4）を NAS に送ってダウンロードできます。',
      'options.howToUse.title': '📖 使い方:',
      'options.howToUse.step1': '上で NAS エンドポイントと API キーを設定',
      'options.howToUse.step2': '動画配信サイトを閲覧',
      'options.howToUse.step3': '動画 URL（M3U8、MP4）が自動検出されます',
      'options.howToUse.step4': '拡張機能アイコンから検出結果を確認',
      'options.howToUse.step5': '「NAS に送信」でダウンロード開始',
      'options.needHelp.title': '🔧 ヘルプ',
      'options.needHelp.bodyHtml': '• この PC から NAS にアクセスできるか確認<br>• API ポートが正しいか確認（既定: 52052; API コンテナ :8000 にマップ）<br>• API キーが NAS 設定と一致するか確認<br>• NAS のファイアウォール設定を確認',
      'options.footer': 'WebVideo2NAS © 2025',

      'options.status.enterNasEndpoint': 'NAS エンドポイント URL を入力してください',
      'options.status.enterApiKey': 'API キーを入力してください',
      'options.status.invalidUrl': 'URL の形式が不正です。http:// または https:// を使用してください',
      'options.status.saved': '✅ 設定を保存しました！',
      'options.status.enterBoth': 'NAS エンドポイントと API キーを入力してください',
      'options.status.testing': '🔍 接続テスト中...',
      'options.status.connectionOk': '✅ 接続成功！NAS に到達できます。',
      'options.status.connectedWithStats': '✅ 接続済み！アクティブ: {active}、キュー: {queue}',
      'options.status.unexpectedResponse': 'NAS から想定外の応答が返りました',
      'options.status.connectionFailedPrefix': '❌ 接続失敗: ',
      'options.status.cannotReach': 'NAS に到達できません。IP とポートを確認してください。',
      'options.status.invalidApiKey': 'API キーが無効です。',
      'options.status.apiNotFound': 'API エンドポイントが見つかりません。NAS 設定を確認してください。'
    },
    ko: {
      'status.checking': '확인 중...',
      'status.notConfigured': '설정되지 않음',
      'status.connected': '연결됨',
      'status.disconnected': '연결 끊김',

      'btn.refresh.title': '새로고침',
      'btn.settings.title': '설정',
      'section.detectedVideos': '감지된 동영상',
      'section.recentDownloads': 'NAS 최근 다운로드',
      'empty.noVideos.title': '🔍 아직 감지된 동영상이 없습니다',
      'empty.noVideos.hint': '동영상 스트리밍 사이트로 이동해 보세요',
      'empty.noJobs.title': '📥 NAS에 최근 다운로드가 없습니다',
      'empty.noJobs.short': '최근 다운로드 없음',

      'url.ipWarning.title': 'IP 제한 URL 감지됨',
      'url.ipWarning.body': '이 URL에는 IP 주소가 포함되어 있어, 사이트가 해당 IP에서만 다운로드를 허용할 수 있습니다.\n성공적으로 다운로드하려면 NAS와 PC가 같은 IP 주소를 사용해야 합니다.\nTailscale Exit Node 또는 유사한 VPN 솔루션으로 트래픽을 동일한 IP로 라우팅하세요.',
      'url.nowPlaying': '재생 중',
      'url.sendToNas': 'NAS로 보내기',
      'url.copy': '복사',

      'job.duration': '소요 시간: {duration}',
      'job.cancel.title': '다운로드 취소',
      'job.cancel': '취소',
      'job.solution': '권장 해결 방법',

      'alert.configureFirst': '먼저 NAS 설정을 완료해 주세요',
      'video.untitled': '제목 없는 동영상',
      'toast.sending': 'NAS로 전송 중...',
      'toast.failedToSend': '전송 실패',
      'toast.nasNotConfigured': '❌ NAS가 설정되지 않았습니다',
      'toast.jobCancelled': '작업이 취소되었습니다',
      'toast.failedToCancel': '취소 실패',
      'toast.copied': '클립보드에 복사됨',

      'jobStatus.pending': '대기 중',
      'jobStatus.downloading': '다운로드 중',
      'jobStatus.processing': '처리 중',
      'jobStatus.completed': '완료',
      'jobStatus.failed': '실패',
      'jobStatus.cancelled': '취소됨',

      'error.unknown.type': '알 수 없는 오류',
      'error.unknown.message': '사용 가능한 오류 세부 정보가 없습니다',
      'error.unknown.solution': '다시 시도하거나 NAS 로그에서 자세한 정보를 확인하세요.',

      'error.403.type': '접근 거부 (403)',
      'error.403.solution': '이 웹사이트는 <strong>IP 기반 인증</strong>을 사용할 가능성이 있습니다. 동영상 URL은 PC의 IP 주소에 대해 생성되었지만 NAS의 IP가 다릅니다.\n<ul>\n  <li><strong>Tailscale Exit Node</strong>를 사용해 NAS 트래픽을 PC를 통해 라우팅</li>\n  <li>NAS 대신 로컬 PC에서 downloader 실행</li>\n  <li>VPN으로 두 장치가 동일한 공인 IP를 사용하도록 설정</li>\n</ul>',

      'error.404.type': '찾을 수 없음 (404)',
      'error.404.solution': '동영상 URL이 더 이상 유효하지 않습니다.\n<ul>\n  <li>URL이 만료됨</li>\n  <li>동영상이 삭제됨</li>\n  <li>링크가 임시이며 새로고침이 필요함</li>\n</ul>\n동영상 페이지를 새로고침한 뒤 다시 전송해 보세요.',

      'error.timeout.type': '연결 시간 초과',
      'error.timeout.solution': '동영상 서버 연결이 시간 초과되었습니다.\n<ul>\n  <li>NAS 네트워크 연결을 확인하세요</li>\n  <li>동영상 서버가 느리거나 과부하일 수 있습니다</li>\n  <li>나중에 다시 시도하세요</li>\n</ul>',

      'error.ssl.type': 'SSL/TLS 오류',
      'error.ssl.solution': '보안 연결에 문제가 발생했습니다.\n<ul>\n  <li>NAS 시스템 시간이 정확한지 확인하세요</li>\n  <li>웹사이트 인증서가 유효하지 않을 수 있습니다</li>\n  <li>downloader를 최신 버전으로 업데이트해 보세요</li>\n</ul>',

      'error.connection.type': '연결 오류',
      'error.connection.solution': '동영상 서버에 연결할 수 없습니다.\n<ul>\n  <li>NAS 인터넷 연결을 확인하세요</li>\n  <li>동영상 서버가 다운되었을 수 있습니다</li>\n  <li>NAS가 외부 웹사이트에 접근 가능한지 확인하세요</li>\n</ul>',

      'error.invalidPlaylist.type': '유효하지 않은 재생목록',
      'error.invalidPlaylist.solution': 'm3u8 재생목록이 비어 있거나 유효하지 않습니다.\n<ul>\n  <li>동영상에 인증/로그인이 필요할 수 있습니다</li>\n  <li>재생목록 URL이 불완전할 수 있습니다</li>\n  <li>동영상 형식이 지원되지 않을 수 있습니다</li>\n</ul>',

      'error.generic.type': '다운로드 실패',
      'error.generic.solution': '다운로드 중 오류가 발생했습니다.\n<ul>\n  <li>NAS 로그에서 자세한 정보를 확인하세요</li>\n  <li>동영상 페이지를 새로고침한 뒤 다시 전송해 보세요</li>\n  <li>일부 사이트는 다운로드 방지 기능이 있어 우회가 불가능할 수 있습니다</li>\n</ul>',

      'options.pageTitle': 'WebVideo2NAS 설정',
      'options.subtitle': 'NAS 연결을 설정하세요',
      'options.nasConfig.title': 'NAS 설정',
      'options.nasEndpoint.label': 'NAS 엔드포인트 URL',
      'options.nasEndpoint.helpHtml': '형식: <code>http://YOUR_NAS_IP:PORT</code><br>예: <code>http://192.168.50.181:52052</code>',
      'options.apiKey.label': 'API 키',
      'options.apiKey.helpHtml': 'NAS의 <code>.env</code> 파일에서 확인할 수 있습니다',
      'options.btn.test': '연결 테스트',
      'options.btn.save': '설정 저장',
      'options.downloadPreferences.title': '다운로드 환경설정',
      'options.uiLanguage.label': '사이드 패널 언어',
      'options.uiLanguage.auto': '자동(브라우저 언어)',
      'options.uiLanguage.help': '사이드 패널 UI에 적용됩니다.',
      'options.autoDetect.label': '동영상 URL 자동 감지',
      'options.autoDetect.help': '탐색 중 M3U8/MP4 URL을 자동으로 감지합니다',
      'options.showNotifications.label': '알림 표시',
      'options.showNotifications.help': '다운로드 제출 시 알림을 표시합니다',
      'options.about.title': '정보',
      'options.about.version': '버전:',
      'options.about.author': '작성자:',
      'options.about.authorValue': 'WebVideo2NAS Team',
      'options.about.description': '이 확장 프로그램은 웹 동영상(M3U8 스트림, MP4 파일)을 NAS로 보내 다운로드할 수 있게 해줍니다.',
      'options.howToUse.title': '📖 사용 방법:',
      'options.howToUse.step1': '위에서 NAS 엔드포인트와 API 키를 설정하세요',
      'options.howToUse.step2': '동영상 스트리밍 사이트로 이동하세요',
      'options.howToUse.step3': '동영상 URL(M3U8, MP4)이 자동으로 감지됩니다',
      'options.howToUse.step4': '확장 프로그램 아이콘을 클릭해 감지된 URL을 확인하세요',
      'options.howToUse.step5': '“NAS로 보내기”를 눌러 다운로드를 시작하세요',
      'options.needHelp.title': '🔧 도움이 필요하신가요?',
      'options.needHelp.bodyHtml': '• 이 PC에서 NAS에 접근 가능한지 확인하세요<br>• API 포트가 올바른지 확인하세요(기본: 52052; API 컨테이너 :8000에 매핑)<br>• API 키가 NAS 설정과 일치하는지 확인하세요<br>• NAS 방화벽 설정을 확인하세요',
      'options.footer': 'WebVideo2NAS © 2025',

      'options.status.enterNasEndpoint': 'NAS 엔드포인트 URL을 입력하세요',
      'options.status.enterApiKey': 'API 키를 입력하세요',
      'options.status.invalidUrl': 'URL 형식이 올바르지 않습니다. http:// 또는 https:// 를 사용하세요',
      'options.status.saved': '✅ 설정이 저장되었습니다!',
      'options.status.enterBoth': 'NAS 엔드포인트와 API 키를 모두 입력하세요',
      'options.status.testing': '🔍 연결 테스트 중...',
      'options.status.connectionOk': '✅ 연결 성공! NAS에 접근할 수 있습니다.',
      'options.status.connectedWithStats': '✅ 연결됨! 활성 다운로드: {active}, 대기열: {queue}',
      'options.status.unexpectedResponse': 'NAS로부터 예상치 못한 응답을 받았습니다',
      'options.status.connectionFailedPrefix': '❌ 연결 실패: ',
      'options.status.cannotReach': 'NAS에 연결할 수 없습니다. IP와 포트를 확인하세요.',
      'options.status.invalidApiKey': 'API 키가 올바르지 않습니다.',
      'options.status.apiNotFound': 'API 엔드포인트를 찾을 수 없습니다. NAS 설정을 확인하세요.'
    },
    fr: {
      'status.checking': 'Vérification…',
      'status.notConfigured': 'Non configuré',
      'status.connected': 'Connecté',
      'status.disconnected': 'Déconnecté',

      'btn.refresh.title': 'Rafraîchir',
      'btn.settings.title': 'Paramètres',
      'section.detectedVideos': 'Vidéos détectées',
      'section.recentDownloads': 'Téléchargements récents sur le NAS',
      'empty.noVideos.title': '🔍 Aucune vidéo détectée pour le moment',
      'empty.noVideos.hint': 'Ouvrez un site de streaming vidéo',
      'empty.noJobs.title': '📥 Aucun téléchargement récent sur le NAS',
      'empty.noJobs.short': 'Aucun téléchargement récent',

      'url.ipWarning.title': 'URL avec restriction IP détectée',
      'url.ipWarning.body': 'Cette URL contient une adresse IP, ce qui peut indiquer une restriction de téléchargement à cette IP.\nPour réussir, votre NAS et votre PC doivent utiliser la même IP.\nUtilisez un Exit Node Tailscale ou un VPN similaire pour router le trafic via la même IP.',
      'url.nowPlaying': 'En cours de lecture',
      'url.sendToNas': 'Envoyer au NAS',
      'url.copy': 'Copier',

      'job.duration': 'Durée : {duration}',
      'job.cancel.title': 'Annuler le téléchargement',
      'job.cancel': 'Annuler',
      'job.solution': 'Solution suggérée',

      'alert.configureFirst': 'Veuillez d’abord configurer le NAS',
      'video.untitled': 'Vidéo sans titre',
      'toast.sending': 'Envoi au NAS…',
      'toast.failedToSend': 'Échec de l’envoi',
      'toast.nasNotConfigured': '❌ NAS non configuré',
      'toast.jobCancelled': 'Tâche annulée',
      'toast.failedToCancel': 'Échec de l’annulation',
      'toast.copied': 'Copié dans le presse-papiers',

      'jobStatus.pending': 'En attente',
      'jobStatus.downloading': 'Téléchargement',
      'jobStatus.processing': 'Traitement',
      'jobStatus.completed': 'Terminé',
      'jobStatus.failed': 'Échec',
      'jobStatus.cancelled': 'Annulé',

      'error.unknown.type': 'Erreur inconnue',
      'error.unknown.message': 'Aucun détail d’erreur disponible',
      'error.unknown.solution': 'Réessayez ou consultez les logs du NAS.',

      'error.403.type': 'Accès refusé (403)',
      'error.403.solution': 'Ce site utilise probablement une <strong>authentification basée sur l’IP</strong>. L’URL a été générée pour l’IP de votre PC, mais votre NAS a une autre IP.\n<ul>\n  <li>Utilisez un <strong>Exit Node Tailscale</strong> pour faire passer le trafic du NAS par votre PC</li>\n  <li>Exécutez le downloader sur votre PC plutôt que sur le NAS</li>\n  <li>Utilisez un VPN pour partager la même IP publique</li>\n</ul>',

      'error.404.type': 'Introuvable (404)',
      'error.404.solution': 'L’URL de la vidéo n’est plus valide.\n<ul>\n  <li>L’URL a expiré</li>\n  <li>La vidéo a été supprimée</li>\n  <li>Le lien est temporaire et doit être rafraîchi</li>\n</ul>\nRafraîchissez la page vidéo et renvoyez une demande.',

      'error.timeout.type': 'Délai dépassé',
      'error.timeout.solution': 'La connexion au serveur vidéo a expiré.\n<ul>\n  <li>Vérifiez la connexion réseau de votre NAS</li>\n  <li>Le serveur vidéo peut être lent ou surchargé</li>\n  <li>Réessayez plus tard</li>\n</ul>',

      'error.ssl.type': 'Erreur SSL/TLS',
      'error.ssl.solution': 'Problème avec la connexion sécurisée.\n<ul>\n  <li>Vérifiez l’heure système du NAS</li>\n  <li>Le certificat du site peut être invalide</li>\n  <li>Essayez de mettre à jour le downloader</li>\n</ul>',

      'error.connection.type': 'Erreur de connexion',
      'error.connection.solution': 'Impossible de se connecter au serveur vidéo.\n<ul>\n  <li>Vérifiez la connexion Internet du NAS</li>\n  <li>Le serveur vidéo peut être indisponible</li>\n  <li>Vérifiez que le NAS peut accéder à Internet</li>\n</ul>',

      'error.invalidPlaylist.type': 'Playlist invalide',
      'error.invalidPlaylist.solution': 'La playlist m3u8 est vide ou invalide.\n<ul>\n  <li>La vidéo nécessite une authentification</li>\n  <li>L’URL est incomplète</li>\n  <li>Format non supporté</li>\n</ul>',

      'error.generic.type': 'Téléchargement échoué',
      'error.generic.solution': 'Une erreur est survenue pendant le téléchargement.\n<ul>\n  <li>Consultez les logs du NAS</li>\n  <li>Rafraîchissez la page et renvoyez</li>\n  <li>Certaines protections de site ne sont pas contournables</li>\n</ul>',

      'options.pageTitle': 'Paramètres WebVideo2NAS',
      'options.subtitle': 'Configurez la connexion à votre NAS',
      'options.nasConfig.title': 'Configuration du NAS',
      'options.nasEndpoint.label': 'URL du point de terminaison NAS',
      'options.nasEndpoint.helpHtml': 'Format : <code>http://YOUR_NAS_IP:PORT</code><br>Exemple : <code>http://192.168.50.181:52052</code>',
      'options.apiKey.label': 'Clé API',
      'options.apiKey.helpHtml': 'À trouver dans le fichier <code>.env</code> de votre NAS',
      'options.btn.test': 'Tester la connexion',
      'options.btn.save': 'Enregistrer',
      'options.downloadPreferences.title': 'Préférences de téléchargement',
      'options.uiLanguage.label': 'Langue du panneau latéral',
      'options.uiLanguage.auto': 'Auto (langue du navigateur)',
      'options.uiLanguage.help': 'S’applique à l’interface du panneau latéral.',
      'options.autoDetect.label': 'Détecter automatiquement les URL vidéo',
      'options.autoDetect.help': 'Détecter automatiquement les URL M3U8 et MP4 lors de la navigation',
      'options.showNotifications.label': 'Afficher les notifications',
      'options.showNotifications.help': 'Afficher une notification lorsque le téléchargement est envoyé',
      'options.about.title': 'À propos',
      'options.about.version': 'Version :',
      'options.about.author': 'Auteur :',
      'options.about.authorValue': 'Équipe WebVideo2NAS',
      'options.about.description': 'Cette extension vous permet d’envoyer des vidéos web (flux M3U8, fichiers MP4) vers votre NAS pour les télécharger.',
      'options.howToUse.title': '📖 Mode d’emploi :',
      'options.howToUse.step1': 'Configurez ci-dessus l’URL du NAS et la clé API',
      'options.howToUse.step2': 'Accédez à un site de streaming vidéo',
      'options.howToUse.step3': 'Les URL vidéo (M3U8, MP4) seront détectées automatiquement',
      'options.howToUse.step4': 'Cliquez sur l’icône de l’extension pour voir les URL détectées',
      'options.howToUse.step5': 'Cliquez sur « Envoyer au NAS » pour démarrer le téléchargement',
      'options.needHelp.title': '🔧 Besoin d’aide ?',
      'options.needHelp.bodyHtml': '• Vérifiez que votre NAS est accessible depuis cet ordinateur<br>• Vérifiez que le port API est correct (par défaut : 52052 ; mappé sur le conteneur API :8000)<br>• Assurez-vous que la clé API correspond à la configuration du NAS<br>• Vérifiez les paramètres du pare-feu sur votre NAS',
      'options.footer': 'WebVideo2NAS © 2025',

      'options.status.enterNasEndpoint': 'Veuillez saisir l’URL du point de terminaison NAS',
      'options.status.enterApiKey': 'Veuillez saisir la clé API',
      'options.status.invalidUrl': 'Format d’URL invalide. Utilisez http:// ou https://',
      'options.status.saved': '✅ Paramètres enregistrés !',
      'options.status.enterBoth': 'Veuillez saisir l’URL du NAS et la clé API',
      'options.status.testing': '🔍 Test de connexion…',
      'options.status.connectionOk': '✅ Connexion réussie ! Votre NAS est accessible.',
      'options.status.connectedWithStats': '✅ Connecté ! Téléchargements actifs : {active}, File : {queue}',
      'options.status.unexpectedResponse': 'Réponse inattendue du NAS',
      'options.status.connectionFailedPrefix': '❌ Échec de connexion : ',
      'options.status.cannotReach': 'Impossible d’atteindre le NAS. Vérifiez l’adresse IP et le port.',
      'options.status.invalidApiKey': 'Clé API invalide.',
      'options.status.apiNotFound': 'Point de terminaison API introuvable. Vérifiez la configuration du NAS.'
    },
    es: {
      'status.checking': 'Comprobando…',
      'status.notConfigured': 'Sin configurar',
      'status.connected': 'Conectado',
      'status.disconnected': 'Desconectado',

      'btn.refresh.title': 'Actualizar',
      'btn.settings.title': 'Ajustes',
      'section.detectedVideos': 'Videos detectados',
      'section.recentDownloads': 'Descargas recientes en el NAS',
      'empty.noVideos.title': '🔍 Aún no se detectaron videos',
      'empty.noVideos.hint': 'Navega a un sitio de streaming',
      'empty.noJobs.title': '📥 No hay descargas recientes en el NAS',
      'empty.noJobs.short': 'No hay descargas recientes',

      'url.ipWarning.title': 'URL con restricción por IP detectada',
      'url.ipWarning.body': 'Esta URL contiene una dirección IP, lo que puede indicar que el sitio restringe la descarga a esa IP.\nPara descargar con éxito, tu NAS y tu PC deben usar la misma IP.\nUsa un Exit Node de Tailscale o una VPN similar para enrutar el tráfico por la misma IP.',
      'url.nowPlaying': 'Reproduciendo',
      'url.sendToNas': 'Enviar al NAS',
      'url.copy': 'Copiar',

      'job.duration': 'Duración: {duration}',
      'job.cancel.title': 'Cancelar descarga',
      'job.cancel': 'Cancelar',
      'job.solution': 'Solución sugerida',

      'alert.configureFirst': 'Primero configura el NAS',
      'video.untitled': 'Video sin título',
      'toast.sending': 'Enviando al NAS…',
      'toast.failedToSend': 'Error al enviar',
      'toast.nasNotConfigured': '❌ NAS sin configurar',
      'toast.jobCancelled': 'Trabajo cancelado',
      'toast.failedToCancel': 'Error al cancelar',
      'toast.copied': 'Copiado al portapapeles',

      'jobStatus.pending': 'Pendiente',
      'jobStatus.downloading': 'Descargando',
      'jobStatus.processing': 'Procesando',
      'jobStatus.completed': 'Completado',
      'jobStatus.failed': 'Fallido',
      'jobStatus.cancelled': 'Cancelado',

      'error.unknown.type': 'Error desconocido',
      'error.unknown.message': 'No hay detalles de error disponibles',
      'error.unknown.solution': 'Inténtalo de nuevo o revisa los logs del NAS.',

      'error.403.type': 'Acceso denegado (403)',
      'error.403.solution': 'Este sitio probablemente usa <strong>autenticación basada en IP</strong>. La URL se generó para la IP de tu PC, pero tu NAS tiene otra IP.\n<ul>\n  <li>Usa un <strong>Exit Node de Tailscale</strong> para enrutar el tráfico del NAS por tu PC</li>\n  <li>Ejecuta el downloader en tu PC en lugar del NAS</li>\n  <li>Usa una VPN para que ambos tengan la misma IP pública</li>\n</ul>',

      'error.404.type': 'No encontrado (404)',
      'error.404.solution': 'La URL del video ya no es válida.\n<ul>\n  <li>La URL expiró</li>\n  <li>El video fue eliminado</li>\n  <li>El enlace es temporal y debe actualizarse</li>\n</ul>\nActualiza la página del video y vuelve a enviarlo.',

      'error.timeout.type': 'Tiempo de espera agotado',
      'error.timeout.solution': 'La conexión al servidor de video expiró.\n<ul>\n  <li>Verifica la conexión de red de tu NAS</li>\n  <li>El servidor puede estar lento o saturado</li>\n  <li>Intenta más tarde</li>\n</ul>',

      'error.ssl.type': 'Error SSL/TLS',
      'error.ssl.solution': 'Hubo un problema con la conexión segura.\n<ul>\n  <li>Verifica la hora del sistema del NAS</li>\n  <li>El sitio puede tener un certificado inválido</li>\n  <li>Intenta actualizar el downloader</li>\n</ul>',

      'error.connection.type': 'Error de conexión',
      'error.connection.solution': 'No se pudo conectar al servidor de video.\n<ul>\n  <li>Verifica la conexión a Internet del NAS</li>\n  <li>El servidor podría estar caído</li>\n  <li>Verifica que el NAS pueda acceder a sitios externos</li>\n</ul>',

      'error.invalidPlaylist.type': 'Playlist inválida',
      'error.invalidPlaylist.solution': 'La playlist m3u8 está vacía o es inválida.\n<ul>\n  <li>El video requiere autenticación</li>\n  <li>La URL de la playlist está incompleta</li>\n  <li>El formato no es compatible</li>\n</ul>',

      'error.generic.type': 'Descarga fallida',
      'error.generic.solution': 'Ocurrió un error durante la descarga.\n<ul>\n  <li>Revisa los logs del NAS</li>\n  <li>Actualiza la página y reenvía</li>\n  <li>Algunos sitios tienen protección que no se puede evitar</li>\n</ul>',

      'options.pageTitle': 'Configuración de WebVideo2NAS',
      'options.subtitle': 'Configura la conexión con tu NAS',
      'options.nasConfig.title': 'Configuración del NAS',
      'options.nasEndpoint.label': 'URL del endpoint del NAS',
      'options.nasEndpoint.helpHtml': 'Formato: <code>http://YOUR_NAS_IP:PORT</code><br>Ejemplo: <code>http://192.168.50.181:52052</code>',
      'options.apiKey.label': 'Clave API',
      'options.apiKey.helpHtml': 'Encuéntralo en el archivo <code>.env</code> de tu NAS',
      'options.btn.test': 'Probar conexión',
      'options.btn.save': 'Guardar configuración',
      'options.downloadPreferences.title': 'Preferencias de descarga',
      'options.uiLanguage.label': 'Idioma del panel lateral',
      'options.uiLanguage.auto': 'Auto (idioma del navegador)',
      'options.uiLanguage.help': 'Se aplica a la interfaz del panel lateral.',
      'options.autoDetect.label': 'Detectar URLs de video automáticamente',
      'options.autoDetect.help': 'Detecta automáticamente URLs M3U8 y MP4 al navegar',
      'options.showNotifications.label': 'Mostrar notificaciones',
      'options.showNotifications.help': 'Mostrar notificaciones cuando se envía la descarga',
      'options.about.title': 'Acerca de',
      'options.about.version': 'Versión:',
      'options.about.author': 'Autor:',
      'options.about.authorValue': 'Equipo de WebVideo2NAS',
      'options.about.description': 'Esta extensión te permite enviar videos web (streams M3U8, archivos MP4) a tu NAS para descargarlos.',
      'options.howToUse.title': '📖 Cómo usar:',
      'options.howToUse.step1': 'Configura arriba el endpoint del NAS y la clave API',
      'options.howToUse.step2': 'Navega a un sitio de streaming de video',
      'options.howToUse.step3': 'Las URLs de video (M3U8, MP4) se detectarán automáticamente',
      'options.howToUse.step4': 'Haz clic en el icono de la extensión para ver las URLs detectadas',
      'options.howToUse.step5': 'Haz clic en «Enviar al NAS» para comenzar la descarga',
      'options.needHelp.title': '🔧 ¿Necesitas ayuda?',
      'options.needHelp.bodyHtml': '• Comprueba que tu NAS sea accesible desde este equipo<br>• Verifica que el puerto de la API sea correcto (por defecto: 52052; mapeado al contenedor API :8000)<br>• Asegúrate de que la clave API coincida con la configuración del NAS<br>• Revisa la configuración del firewall en tu NAS',
      'options.footer': 'WebVideo2NAS © 2025',

      'options.status.enterNasEndpoint': 'Introduce la URL del endpoint del NAS',
      'options.status.enterApiKey': 'Introduce la clave API',
      'options.status.invalidUrl': 'Formato de URL inválido. Usa http:// o https://',
      'options.status.saved': '✅ ¡Configuración guardada!',
      'options.status.enterBoth': 'Introduce el endpoint del NAS y la clave API',
      'options.status.testing': '🔍 Probando conexión…',
      'options.status.connectionOk': '✅ ¡Conexión exitosa! Tu NAS es accesible.',
      'options.status.connectedWithStats': '✅ ¡Conectado! Descargas activas: {active}, Cola: {queue}',
      'options.status.unexpectedResponse': 'Respuesta inesperada del NAS',
      'options.status.connectionFailedPrefix': '❌ Error de conexión: ',
      'options.status.cannotReach': 'No se puede acceder al NAS. Revisa la IP y el puerto.',
      'options.status.invalidApiKey': 'Clave API inválida.',
      'options.status.apiNotFound': 'No se encontró el endpoint de la API. Revisa la configuración del NAS.'
    },
    pt: {
      'status.checking': 'Verificando…',
      'status.notConfigured': 'Não configurado',
      'status.connected': 'Conectado',
      'status.disconnected': 'Desconectado',

      'btn.refresh.title': 'Atualizar',
      'btn.settings.title': 'Configurações',
      'section.detectedVideos': 'Vídeos detectados',
      'section.recentDownloads': 'Downloads recentes no NAS',
      'empty.noVideos.title': '🔍 Nenhum vídeo detectado ainda',
      'empty.noVideos.hint': 'Acesse um site de streaming de vídeo',
      'empty.noJobs.title': '📥 Nenhum download recente no NAS',
      'empty.noJobs.short': 'Nenhum download recente',

      'url.ipWarning.title': 'URL com restrição de IP detectada',
      'url.ipWarning.body': 'Esta URL contém um endereço IP, o que pode indicar que o site restringe o download a esse IP.\nPara baixar com sucesso, seu NAS e seu PC precisam usar o mesmo IP.\nUse um Exit Node do Tailscale ou uma VPN similar para rotear o tráfego pelo mesmo IP.',
      'url.nowPlaying': 'Reproduzindo',
      'url.sendToNas': 'Enviar ao NAS',
      'url.copy': 'Copiar',

      'job.duration': 'Duração: {duration}',
      'job.cancel.title': 'Cancelar download',
      'job.cancel': 'Cancelar',
      'job.solution': 'Solução sugerida',

      'alert.configureFirst': 'Configure o NAS primeiro',
      'video.untitled': 'Vídeo sem título',
      'toast.sending': 'Enviando ao NAS…',
      'toast.failedToSend': 'Falha ao enviar',
      'toast.nasNotConfigured': '❌ NAS não configurado',
      'toast.jobCancelled': 'Tarefa cancelada',
      'toast.failedToCancel': 'Falha ao cancelar',
      'toast.copied': 'Copiado para a área de transferência',

      'jobStatus.pending': 'Pendente',
      'jobStatus.downloading': 'Baixando',
      'jobStatus.processing': 'Processando',
      'jobStatus.completed': 'Concluído',
      'jobStatus.failed': 'Falhou',
      'jobStatus.cancelled': 'Cancelado',

      'error.unknown.type': 'Erro desconhecido',
      'error.unknown.message': 'Sem detalhes de erro disponíveis',
      'error.unknown.solution': 'Tente novamente ou verifique os logs do NAS.',

      'error.403.type': 'Acesso negado (403)',
      'error.403.solution': 'Este site provavelmente usa <strong>autenticação baseada em IP</strong>. A URL foi gerada para o IP do seu PC, mas seu NAS tem outro IP.\n<ul>\n  <li>Use um <strong>Exit Node do Tailscale</strong> para rotear o tráfego do NAS pelo seu PC</li>\n  <li>Execute o downloader no seu PC em vez do NAS</li>\n  <li>Use uma VPN para que ambos tenham o mesmo IP público</li>\n</ul>',

      'error.404.type': 'Não encontrado (404)',
      'error.404.solution': 'A URL do vídeo não é mais válida.\n<ul>\n  <li>A URL expirou</li>\n  <li>O vídeo foi removido</li>\n  <li>O link é temporário e precisa ser atualizado</li>\n</ul>\nAtualize a página do vídeo e envie novamente.',

      'error.timeout.type': 'Tempo esgotado',
      'error.timeout.solution': 'A conexão com o servidor de vídeo expirou.\n<ul>\n  <li>Verifique a conexão de rede do NAS</li>\n  <li>O servidor pode estar lento ou sobrecarregado</li>\n  <li>Tente mais tarde</li>\n</ul>',

      'error.ssl.type': 'Erro SSL/TLS',
      'error.ssl.solution': 'Houve um problema com a conexão segura.\n<ul>\n  <li>Verifique se o horário do NAS está correto</li>\n  <li>O site pode ter um certificado inválido</li>\n  <li>Tente atualizar o downloader</li>\n</ul>',

      'error.connection.type': 'Erro de conexão',
      'error.connection.solution': 'Não foi possível conectar ao servidor de vídeo.\n<ul>\n  <li>Verifique a conexão com a internet do NAS</li>\n  <li>O servidor pode estar fora do ar</li>\n  <li>Verifique se o NAS acessa sites externos</li>\n</ul>',

      'error.invalidPlaylist.type': 'Playlist inválida',
      'error.invalidPlaylist.solution': 'A playlist m3u8 está vazia ou inválida.\n<ul>\n  <li>O vídeo requer autenticação</li>\n  <li>A URL da playlist está incompleta</li>\n  <li>O formato não é suportado</li>\n</ul>',

      'error.generic.type': 'Falha no download',
      'error.generic.solution': 'Ocorreu um erro durante o download.\n<ul>\n  <li>Verifique os logs do NAS</li>\n  <li>Atualize a página e reenviar</li>\n  <li>Alguns sites têm proteção que não pode ser contornada</li>\n</ul>',

      'options.pageTitle': 'Configurações do WebVideo2NAS',
      'options.subtitle': 'Configure sua conexão com o NAS',
      'options.nasConfig.title': 'Configuração do NAS',
      'options.nasEndpoint.label': 'URL do endpoint do NAS',
      'options.nasEndpoint.helpHtml': 'Formato: <code>http://YOUR_NAS_IP:PORT</code><br>Exemplo: <code>http://192.168.50.181:52052</code>',
      'options.apiKey.label': 'Chave de API',
      'options.apiKey.helpHtml': 'Encontre isso no arquivo <code>.env</code> do seu NAS',
      'options.btn.test': 'Testar conexão',
      'options.btn.save': 'Salvar configurações',
      'options.downloadPreferences.title': 'Preferências de download',
      'options.uiLanguage.label': 'Idioma do painel lateral',
      'options.uiLanguage.auto': 'Auto (idioma do navegador)',
      'options.uiLanguage.help': 'Aplica-se à interface do painel lateral.',
      'options.autoDetect.label': 'Detectar URLs de vídeo automaticamente',
      'options.autoDetect.help': 'Detecta automaticamente URLs M3U8 e MP4 ao navegar',
      'options.showNotifications.label': 'Mostrar notificações',
      'options.showNotifications.help': 'Mostrar notificações quando o download for enviado',
      'options.about.title': 'Sobre',
      'options.about.version': 'Versão:',
      'options.about.author': 'Autor:',
      'options.about.authorValue': 'Equipe WebVideo2NAS',
      'options.about.description': 'Esta extensão permite enviar vídeos da web (streams M3U8, arquivos MP4) para o seu NAS para download.',
      'options.howToUse.title': '📖 Como usar:',
      'options.howToUse.step1': 'Configure acima o endpoint do NAS e a chave de API',
      'options.howToUse.step2': 'Navegue até um site de streaming de vídeo',
      'options.howToUse.step3': 'As URLs de vídeo (M3U8, MP4) serão detectadas automaticamente',
      'options.howToUse.step4': 'Clique no ícone da extensão para ver as URLs detectadas',
      'options.howToUse.step5': 'Clique em “Enviar ao NAS” para iniciar o download',
      'options.needHelp.title': '🔧 Precisa de ajuda?',
      'options.needHelp.bodyHtml': '• Verifique se o NAS está acessível a partir deste computador<br>• Verifique se a porta da API está correta (padrão: 52052; mapeada para o contêiner da API :8000)<br>• Garanta que a chave de API corresponda à configuração do NAS<br>• Verifique as configurações do firewall no seu NAS',
      'options.footer': 'WebVideo2NAS © 2025',

      'options.status.enterNasEndpoint': 'Digite a URL do endpoint do NAS',
      'options.status.enterApiKey': 'Digite a chave de API',
      'options.status.invalidUrl': 'Formato de URL inválido. Use http:// ou https://',
      'options.status.saved': '✅ Configurações salvas!',
      'options.status.enterBoth': 'Digite o endpoint do NAS e a chave de API',
      'options.status.testing': '🔍 Testando conexão…',
      'options.status.connectionOk': '✅ Conexão bem-sucedida! Seu NAS está acessível.',
      'options.status.connectedWithStats': '✅ Conectado! Downloads ativos: {active}, Fila: {queue}',
      'options.status.unexpectedResponse': 'Resposta inesperada do NAS',
      'options.status.connectionFailedPrefix': '❌ Falha na conexão: ',
      'options.status.cannotReach': 'Não foi possível acessar o NAS. Verifique o IP e a porta.',
      'options.status.invalidApiKey': 'Chave de API inválida.',
      'options.status.apiNotFound': 'Endpoint da API não encontrado. Verifique a configuração do NAS.'
    }
  };

  let currentLang = DEFAULT_LANG;

  function setLanguage(lang) {
    const normalized = normalizeLang(lang) || detectDefaultLanguage();
    currentLang = SUPPORTED_LANGS.includes(normalized) ? normalized : DEFAULT_LANG;
    if (document && document.documentElement) {
      document.documentElement.lang = currentLang;
    }
    return currentLang;
  }

  function formatTemplate(str, vars) {
    if (!vars) return str;
    return String(str).replace(/\{([a-zA-Z0-9_]+)\}/g, (m, key) => {
      if (Object.prototype.hasOwnProperty.call(vars, key)) return String(vars[key]);
      return m;
    });
  }

  function t(key, vars) {
    const dict = MESSAGES[currentLang] || MESSAGES[DEFAULT_LANG] || {};
    const base = MESSAGES[DEFAULT_LANG] || {};
    const raw = (dict && dict[key]) || base[key] || key;
    return formatTemplate(raw, vars);
  }

  function tHtml(key, vars) {
    // Same as t(), but keeps embedded HTML.
    return t(key, vars).replace(/\n/g, '<br>');
  }

  // Initialize once on load.
  setLanguage(detectDefaultLanguage());

  window.WV2N_I18N = {
    SUPPORTED_LANGS,
    normalizeLang,
    detectDefaultLanguage,
    setLanguage,
    t,
    tHtml,
    getLanguage: () => currentLang
  };
})();

