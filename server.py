#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Okul Zil Sistemi - Python Masaustu Uygulamasi
System Tray: pystray + pillow (Baslat.bat tarafindan kurulur)
"""

import http.server, socketserver, webbrowser
import os, sys, json, threading, time, socket, struct, subprocess, platform, secrets
import queue, hashlib, tempfile

# ── Tek Örnek Koruma (Single Instance) ──────────────────────────────────────
_MUTEX_NAME = "NeneOkulZilSistemi_SingleInstance_Mutex_v2"
_app_mutex  = None

def _acquire_single_instance_mutex():
    """Windows named mutex ile tek örnek kontrolü yapar.
    Döndürür: True → bu ilk örnek (devam et), False → başka örnek var (çık)."""
    global _app_mutex
    if platform.system() != "Windows":
        return True  # Windows dışında mutex desteği yok, devam et
    import ctypes
    _app_mutex = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    last_error  = ctypes.windll.kernel32.GetLastError()
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        return False
    return True
# ─────────────────────────────────────────────────────────────────────────────

PORT       = 8765
HTTPS_PORT = 8766   # Mikrofon (getUserMedia) için HTTPS portu
SCRIPT_DIR = os.environ.get("ZIL_DIR", os.path.dirname(os.path.abspath(__file__))).rstrip("\\/")
os.chdir(SCRIPT_DIR)

# ── WebView2 Autoplay Kilidi — Process Başlangıcında ────────────────────────
# WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS, WebView2 runtime başlamadan ÖNCE
# set edilmeli. open_window() içinde yapmak bazı sistemlerde geç kalıyor.
_AUTOPLAY_ARGS = (
    "--autoplay-policy=no-user-gesture-required "
    "--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies,"
    "BackForwardCache,IntensiveWakeUpThrottling,OptimizeLoadingIPH,"
    "CalculateNativeWinOcclusion,HeavyAdIntervention "
    "--disable-background-timer-throttling "
    "--disable-backgrounding-occluded-windows "
    "--disable-renderer-backgrounding "
    "--disable-background-media-suspend "
    "--renderer-process-limit=100"
)
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = _AUTOPLAY_ARGS

# Yöntem 1: WebView2 user-data klasörünü AppData altına yönlendir.
# Program klasöründe _chrome_profile oluşmaz; zip boyutu şişmez.
# Bu klasörde eski profil olmadığından WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
# flag'leri kesinlikle uygulanır (eski profil flag'leri geçersiz kılmaz).
_appdata_early = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
_wv2_profile   = os.path.join(_appdata_early, "NeneOkulZilSistemi", "webview2_profile")
os.makedirs(_wv2_profile, exist_ok=True)
os.environ["WEBVIEW2_USER_DATA_FOLDER"] = _wv2_profile

# Yöntem 2 (yedek): Registry — hem Edge hem WebView2 için autoplay izni yaz.
# HKCU\...\Edge\AutoplayAllowed  → Edge tarayıcı için
# HKCU\...\Edge\WebView2\AdditionalBrowserArguments  → WebView2 runtime için
try:
    import winreg as _wr
    # Edge autoplay
    _key_path = r"Software\Policies\Microsoft\Edge"
    try:
        _k = _wr.OpenKey(_wr.HKEY_CURRENT_USER, _key_path, 0, _wr.KEY_SET_VALUE)
    except FileNotFoundError:
        _k = _wr.CreateKey(_wr.HKEY_CURRENT_USER, _key_path)
    _wr.SetValueEx(_k, "AutoplayAllowed", 0, _wr.REG_DWORD, 1)
    _wr.CloseKey(_k)
    # WebView2 ek argümanlar (tüm origin'ler için)
    _wv2_key_path = r"Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments"
    try:
        _k2 = _wr.OpenKey(_wr.HKEY_CURRENT_USER, _wv2_key_path, 0, _wr.KEY_SET_VALUE)
    except FileNotFoundError:
        _k2 = _wr.CreateKey(_wr.HKEY_CURRENT_USER, _wv2_key_path)
    # "*" → tüm WebView2 origin'leri için geçerli
    _wr.SetValueEx(_k2, "*", 0, _wr.REG_SZ, _AUTOPLAY_ARGS)
    _wr.CloseKey(_k2)
except Exception:
    pass
# ─────────────────────────────────────────────────────────────────────────────

def _win_open_file_dialog(initial_dir="", multiple=False):
    """PowerShell ile native Windows dosya seçici açar."""
    try:
        idir = os.path.normpath(initial_dir) if (initial_dir and os.path.isdir(initial_dir)) else ""
        filter_str = "Ses Dosyalari|*.mp3;*.wav;*.ogg;*.flac;*.aac;*.m4a|Tum Dosyalar|*.*"
        if multiple:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                "$d.Title='Ses Dosyalari Sec';"
                "$d.Filter='" + filter_str + "';"
                "$d.Multiselect=$true;"
                + (f"$d.InitialDirectory='{idir}';" if idir else "") +
                "$d.TopMost=$true;"
                "if($d.ShowDialog() -eq 'OK'){$d.FileNames -join '|'}else{''}"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=120
            )
            out = result.stdout.strip()
            return [p for p in out.split("|") if p and os.path.isfile(p)] if out else []
        else:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                "$d.Title='Ses Dosyasi Sec';"
                "$d.Filter='" + filter_str + "';"
                + (f"$d.InitialDirectory='{idir}';" if idir else "") +
                "$d.TopMost=$true;"
                "if($d.ShowDialog() -eq 'OK'){$d.FileName}else{''}"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=120
            )
            out = result.stdout.strip()
            return out if (out and os.path.isfile(out)) else ""
    except Exception:
        return [] if multiple else ""

# Temiz kapanma için event
_shutdown_event = threading.Event()
_should_exit    = False  # True olunca pencere gercekten kapanir
_silent_mode    = False  # True iken _check_bells zil çalmaz (Sessiz Saatler)
_bell_log: list = []    # Son 200 zil kaydı (yeniden eskiye doğru)
_bell_log_lock  = threading.Lock()

# Ayar dosyaları: Program Files yerine AppData\Roaming altında tutulur
# böylece yönetici yetkisi olmadan da yazılabilir.
_appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
DATA_DIR      = os.path.join(_appdata, "NeneOkulZilSistemi")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")

# Kurulumdan eski konumda kalan dosyaları bir kez taşı (geriye dönük uyumluluk)
for _fname in ("settings.json", "profiles.json"):
    _old = os.path.join(SCRIPT_DIR, _fname)
    _new = os.path.join(DATA_DIR, _fname)
    if os.path.exists(_old) and not os.path.exists(_new):
        try:
            import shutil
            shutil.move(_old, _new)
        except Exception:
            pass
APP_NAME   = "Nene Okul Zil Sistemi"

# ============================================================
# SSL — Kendinden imzalı sertifika (mikrofon için HTTPS gerekli)
# ============================================================
SSL_CERT = os.path.join(DATA_DIR, "zil_cert.pem")
SSL_KEY  = os.path.join(DATA_DIR, "zil_key.pem")

def _ensure_ssl_cert():
    """Sertifika yoksa veya süresi dolmuşsa yeni üret."""
    # Her iki dosya da varsa geç
    if os.path.exists(SSL_CERT) and os.path.exists(SSL_KEY):
        print("[HTTPS] Mevcut SSL sertifikası kullanılıyor.")
        return True
    print("[HTTPS] SSL sertifikası oluşturuluyor…")
    try:
        # cryptography paketi varsa kullan (en güvenilir)
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"OkulZilSistemi")])
        ip = get_local_ip()
        san = x509.SubjectAlternativeName([
            x509.DNSName(u"localhost"),
            x509.IPAddress(__import__('ipaddress').ip_address(ip)),
        ])
        now = datetime.datetime.utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(san, critical=False)
            .sign(key, hashes.SHA256())
        )
        with open(SSL_KEY, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()
            ))
        with open(SSL_CERT, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        print("[HTTPS] SSL sertifikası (cryptography) başarıyla oluşturuldu.")
        return True
    except ImportError:
        print("[HTTPS] 'cryptography' paketi bulunamadı, openssl deneniyor…")
    except Exception as e:
        print(f"[HTTPS] cryptography ile sertifika üretilemedi: {e}")

    # cryptography yoksa — openssl ile dene
    try:
        ip = get_local_ip()
        san_str = f"subjectAltName=DNS:localhost,IP:{ip}"
        result = subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", SSL_KEY, "-out", SSL_CERT,
            "-days", "3650", "-nodes",
            "-subj", "/CN=OkulZilSistemi",
            "-addext", san_str
        ], capture_output=True, timeout=30)
        if result.returncode == 0:
            print("[HTTPS] SSL sertifikası (openssl) başarıyla oluşturuldu.")
            return True
        else:
            print(f"[HTTPS] openssl hatası: {result.stderr.decode(errors='ignore')}")
    except FileNotFoundError:
        print("[HTTPS] openssl bulunamadı.")
    except Exception as e:
        print(f"[HTTPS] openssl ile sertifika üretilemedi: {e}")

    # Son çare — Python'un subprocess ile pyopenssl kurulumu dene
    try:
        print("[HTTPS] pip ile cryptography kuruluyor…")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "cryptography", "--quiet"],
            capture_output=True, timeout=120
        )
        if r.returncode == 0:
            # Tekrar dene
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import datetime
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"OkulZilSistemi")])
            ip = get_local_ip()
            san = x509.SubjectAlternativeName([
                x509.DNSName(u"localhost"),
                x509.IPAddress(__import__('ipaddress').ip_address(ip)),
            ])
            now = datetime.datetime.utcnow()
            cert = (
                x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(san, critical=False)
                .sign(key, hashes.SHA256())
            )
            with open(SSL_KEY, "wb") as f:
                f.write(key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption()
                ))
            with open(SSL_CERT, "wb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))
            print("[HTTPS] SSL sertifikası (pip+cryptography) başarıyla oluşturuldu.")
            return True
    except Exception as e:
        print(f"[HTTPS] pip+cryptography ile sertifika üretilemedi: {e}")

    print("[HTTPS] SSL sertifikası oluşturulamadı — HTTPS devre dışı kalacak.")
    return False  # SSL kurulamadı, HTTP ile devam et


def _is_cert_trusted() -> bool:
    """Sertifikanın Windows 'Güvenilen Kök CA' deposunda kayıtlı olup olmadığını kontrol eder."""
    if not os.path.exists(SSL_CERT) or platform.system() != "Windows":
        return False
    try:
        # certutil ile sertifika parmak izini al
        r = subprocess.run(
            ["certutil", "-dump", SSL_CERT],
            capture_output=True, text=True, timeout=10
        )
        # Parmak izini çıkar
        thumb = None
        for line in r.stdout.splitlines():
            if "Cert Hash(sha1)" in line or "Sertifika Karması(sha1)" in line:
                thumb = line.split(":")[-1].strip().replace(" ", "").lower()
                break
        if not thumb:
            return False
        # Güvenilir kök deposunda aynı parmak izi var mı?
        r2 = subprocess.run(
            ["certutil", "-store", "Root", thumb],
            capture_output=True, text=True, timeout=10
        )
        return r2.returncode == 0 and thumb in r2.stdout.lower().replace(" ", "")
    except Exception:
        return False


def _install_cert_trusted():
    """SSL sertifikasını Windows Güvenilen Kök CA deposuna yükler.
    Yönetici yetkisi gerektirir — UAC ile yükseltilmiş process açılır.
    Sertifika zaten yüklüyse hiçbir şey yapmaz.
    """
    if platform.system() != "Windows":
        return
    if not os.path.exists(SSL_CERT):
        return
    if _is_cert_trusted():
        print("[HTTPS] Sertifika zaten güvenilir depoda kayıtlı.")
        return

    print("[HTTPS] Sertifika güvenilir depoya ekleniyor (UAC onayı gerekebilir)…")
    try:
        import ctypes
        # .crt uzantısıyla geçici kopya oluştur (certutil .pem'i de kabul eder ama .crt daha güvenli)
        crt_path = SSL_CERT.replace(".pem", ".crt")
        import shutil
        shutil.copy2(SSL_CERT, crt_path)

        # UAC ile yükseltilmiş certutil çalıştır
        # ShellExecuteW ile "runas" → UAC penceresi açılır, kullanıcı Evet derse yükler
        result = ctypes.windll.shell32.ShellExecuteW(
            None,                        # hwnd
            "runas",                     # lpOperation  → UAC yükseltme
            "certutil.exe",              # lpFile
            f'-addstore "Root" "{crt_path}"',  # lpParameters
            None,                        # lpDirectory
            1                            # nShowCmd (SW_SHOWNORMAL)
        )
        # ShellExecuteW > 32 ise başarılı
        if result > 32:
            # certutil biraz sürer; en fazla 10 sn bekle
            for _ in range(20):
                time.sleep(0.5)
                if _is_cert_trusted():
                    print("[HTTPS] Sertifika başarıyla güvenilir depoya eklendi.")
                    break
            else:
                print("[HTTPS] Sertifika yükleme doğrulanamadı (kullanıcı iptal etmiş olabilir).")
        else:
            print(f"[HTTPS] ShellExecuteW hatası: {result}")
    except Exception as e:
        print(f"[HTTPS] Sertifika yüklenirken hata: {e}")

    # Firefox'un Windows sertifika deposunu kullanmasını sağla
    # Bu ayar olmadan Firefox kendi deposuna bakar, Windows'a yüklenen sertifikayı tanımaz.
    # HKCU\Software\Policies\Mozilla\Firefox\Certificates\ImportEnterpriseRoots = 1
    # → Firefox, Windows Güvenilen Kök CA deposundaki sertifikalara otomatik güvenir.
    _firefox_enable_enterprise_roots()


def _firefox_enable_enterprise_roots():
    """Firefox'un tüm profillerine sertifikayı doğrudan ekler.
    Firefox'un kendi certutil.exe'sini (NSS) kullanır — registry politikasına gerek yok.
    Firefox kurulu değilse sessizce geçer.
    """
    if platform.system() != "Windows":
        return
    if not os.path.exists(SSL_CERT):
        return

    # 1. Firefox certutil.exe'sini bul (Firefox kurulum klasöründe bulunur)
    ff_certutil = _find_firefox_certutil()
    if not ff_certutil:
        print("[HTTPS/Firefox] Firefox certutil.exe bulunamadı — Firefox kurulu olmayabilir.")
        # Yedek: registry politikası dene (eski yöntem)
        _firefox_policy_fallback()
        return

    # 2. Firefox profil klasörlerini bul
    profiles = _find_firefox_profiles()
    if not profiles:
        print("[HTTPS/Firefox] Firefox profili bulunamadı.")
        return

    # 3. Her profile sertifikayı ekle
    crt_path = SSL_CERT.replace(".pem", ".crt")
    try:
        import shutil
        shutil.copy2(SSL_CERT, crt_path)
    except Exception:
        crt_path = SSL_CERT

    added = 0
    for profile_dir in profiles:
        try:
            result = subprocess.run([
                ff_certutil,
                "-A",                          # Add cert
                "-n", "OkulZilSistemi",        # Nickname
                "-t", "CT,,",                  # Güvenilir CA (C=SSL, T=email, trusted)
                "-i", crt_path,                # Sertifika dosyası
                "-d", f"sql:{profile_dir}",    # Profil dizini (sql: = cert9.db)
            ], capture_output=True, timeout=15)
            if result.returncode == 0:
                added += 1
                print(f"[HTTPS/Firefox] Sertifika eklendi: {os.path.basename(profile_dir)}")
            else:
                # sql: formatı tutmadıysa dbm: dene (eski profiller)
                result2 = subprocess.run([
                    ff_certutil, "-A", "-n", "OkulZilSistemi",
                    "-t", "CT,,", "-i", crt_path,
                    "-d", profile_dir,
                ], capture_output=True, timeout=15)
                if result2.returncode == 0:
                    added += 1
                    print(f"[HTTPS/Firefox] Sertifika eklendi (dbm): {os.path.basename(profile_dir)}")
        except Exception as e:
            print(f"[HTTPS/Firefox] Profil hatası ({os.path.basename(profile_dir)}): {e}")

    if added > 0:
        print(f"[HTTPS/Firefox] Sertifika {added} Firefox profiline eklendi.")
    else:
        print("[HTTPS/Firefox] Hiçbir profile eklenemedi — yedek yöntem deneniyor.")
        _firefox_policy_fallback()


def _find_firefox_certutil():
    """Firefox kurulum klasöründeki certutil.exe'yi döndürür."""
    candidates = [
        r"C:\Program Files\Mozilla Firefox\certutil.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\certutil.exe",
    ]
    # LocalAppData altında kurulu olabilir
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "Mozilla Firefox", "certutil.exe"))

    for path in candidates:
        if os.path.exists(path):
            return path

    # PATH'te var mı? (portable kurulum)
    try:
        r = subprocess.run(["where", "certutil"], capture_output=True, text=True, timeout=5)
        # Windows'un kendi certutil'i değil Firefox'unkini bul
        for line in r.stdout.splitlines():
            line = line.strip()
            if "firefox" in line.lower() or "mozilla" in line.lower():
                return line
    except Exception:
        pass
    return None


def _find_firefox_profiles():
    """Firefox profil dizinlerini döndürür."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return []
    profiles_ini = os.path.join(appdata, "Mozilla", "Firefox", "profiles.ini")
    if not os.path.exists(profiles_ini):
        return []
    try:
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(profiles_ini, encoding="utf-8")
        dirs = []
        ff_base = os.path.join(appdata, "Mozilla", "Firefox")
        for section in cfg.sections():
            if not section.lower().startswith("profile"):
                continue
            path = cfg.get(section, "Path", fallback=None)
            if not path:
                continue
            is_relative = cfg.getint(section, "IsRelative", fallback=1)
            full = os.path.join(ff_base, path.replace("/", os.sep)) if is_relative else path
            if os.path.isdir(full):
                dirs.append(full)
        return dirs
    except Exception as e:
        print(f"[HTTPS/Firefox] profiles.ini okunamadı: {e}")
        return []


def _firefox_policy_fallback():
    """Son çare: registry politikası ile ImportEnterpriseRoots."""
    try:
        import winreg
        key_path = r"Software\Policies\Mozilla\Firefox\Certificates"
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        except FileNotFoundError:
            k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.SetValueEx(k, "ImportEnterpriseRoots", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(k)
        print("[HTTPS/Firefox] Registry politikası yazıldı (yedek yöntem).")
    except Exception as e:
        print(f"[HTTPS/Firefox] Registry politikası yazılamadı: {e}")


_SSL_AVAILABLE  = False  # Başlangıçta bilinmiyor, main'de set edilir
_HTTPS_RUNNING  = False  # HTTPS sunucusu gerçekten dinliyorsa True

# Her başlatmada rastgele güvenlik token'ı üret
API_TOKEN  = secrets.token_hex(16)
APP_URL    = f"http://localhost:{PORT}/index.html?token={API_TOKEN}"

# ============================================================
# UZAKTAN ERİŞİM — PIN, SSE Komut Veri Yolu, Anons Tamponu
# ============================================================
REMOTE_PIN_FILE = os.path.join(DATA_DIR, "remote_pin.json")
ANNOUNCE_DIR    = os.path.join(DATA_DIR, "announces")
os.makedirs(ANNOUNCE_DIR, exist_ok=True)

# Varsayılan PIN hash'i (1234)
_DEFAULT_PIN = "1234"

def _pin_hash(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode()).hexdigest()

def load_remote_pin() -> str:
    """Kayıtlı PIN hash'ini döner. Dosya yoksa varsayılan hash."""
    try:
        with open(REMOTE_PIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("hash", _pin_hash(_DEFAULT_PIN))
    except Exception:
        return _pin_hash(_DEFAULT_PIN)

def save_remote_pin(new_pin: str):
    with open(REMOTE_PIN_FILE, "w", encoding="utf-8") as f:
        json.dump({"hash": _pin_hash(new_pin)}, f)

def check_remote_pin(pin: str) -> bool:
    return _pin_hash(pin) == load_remote_pin()

# SSE: her bağlanan uzak istemci için bir kuyruk
_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()

# Canlı anons streaming yöneticisi (index.html kapalıyken miniaudio ile çalar)
class _LiveAnnounce:
    """
    Cep telefonundan gelen WAV chunk'larini gercek zamanli calar.
    Her chunk geldiginde decode edilip PCM kuyruguna eklenir.
    Ayri bir playback thread'i kuyruktan kesintisiz okuyarak ses calar.
    stream-end gelince kuyruk biter, playback durur.
    """
    # Sabit playback parametreleri — tum chunklarda ayni olmali
    _RATE = 16000
    _NCH  = 1
    _FMT  = None   # miniaudio.SampleFormat.SIGNED16 — import sonrasi set edilir

    def __init__(self):
        self._lock      = threading.Lock()
        self._pcm_q     : queue.Queue = queue.Queue()
        self._mime      : str  = "audio/wav"
        self._active    : bool = False
        self._finished  : bool = False
        self._stop_ev   : threading.Event = threading.Event()
        self._dev       = None
        self._live_rate : int  = 44100   # ilk chunk'tan WAV header'dan okunur
        self._live_nch  : int  = 1

    def start(self, mime: str):
        """stream-start gelince cagirilir."""
        # Onceki oturumu temizle
        self._stop_ev.set()
        time.sleep(0.08)
        self._stop_ev.clear()
        # Kuyrugu bosalt
        while not self._pcm_q.empty():
            try: self._pcm_q.get_nowait()
            except Exception: break
        with self._lock:
            self._mime     = mime
            self._active   = True
            self._finished = False
        self._dev = None
        print(f"[LiveAnons] Kayit basladi — mime: {mime}")
        # Playback thread'ini hemen baslat (ilk chunk gelene kadar bekler)
        threading.Thread(target=self._playback_worker, daemon=True).start()

    def push(self, chunk_bytes: bytes):
        """Her stream-chunk'ta cagirilir — WAV decode edip PCM kuyruğuna ekler."""
        with self._lock:
            if not self._active:
                return
        # Decode
        pcm = self._decode_wav(chunk_bytes)
        if pcm:
            self._pcm_q.put(pcm)

    def stop(self):
        """stream-end gelince cagirilir — playback'e bitis sinyali gonder."""
        with self._lock:
            self._active   = False
            self._finished = True
        # Playback thread'i bos kuyruk + finished=True gorünce durur
        self._pcm_q.put(b"")   # sentinel
        print("[LiveAnons] Stream bitti, kuyruk tükeniyor...")

    def stop_immediate(self):
        """Ani durdurma (örn. başka ses çalmaya başladığında)."""
        self._stop_ev.set()
        with self._lock:
            self._active   = False
            self._finished = True
        while not self._pcm_q.empty():
            try: self._pcm_q.get_nowait()
            except Exception: break
        self._pcm_q.put(b"")
        dev = self._dev
        self._dev = None
        if dev:
            try: dev.stop()
            except Exception: pass
        print("[LiveAnons] Durduruldu.")

    def _decode_wav(self, data: bytes) -> bytes:
        """WAV bytes → ham 16-bit signed PCM bytes. Rate/ch WAV header'dan dinamik okunur."""
        import io, wave
        try:
            with wave.open(io.BytesIO(data)) as w:
                with self._lock:
                    self._live_rate = w.getframerate()
                    self._live_nch  = w.getnchannels()
                return w.readframes(w.getnframes())
        except Exception as e:
            print(f"[LiveAnons] Chunk decode hatası: {e}")
            return b""

    def _playback_worker(self):
        """Ayrı thread — PCM kuyruğundan kesintisiz ses çalar."""
        stop_ev  = self._stop_ev
        self_ref = self
        try:
            import miniaudio

            # İlk PCM chunk'ı bekle — _decode_wav rate/nch'yi set etmiş olur
            first_pcm = None
            while first_pcm is None:
                if stop_ev.is_set():
                    return
                try:
                    first_pcm = self_ref._pcm_q.get(timeout=5.0)
                except queue.Empty:
                    return

            if first_pcm == b"" or stop_ev.is_set():
                return

            with self_ref._lock:
                rate = self_ref._live_rate
                nch  = self_ref._live_nch

            def pcm_gen():
                buf      = first_pcm
                required = yield b""
                while True:
                    if stop_ev.is_set():
                        return
                    need = (required * nch * 2) if required else 4096
                    while len(buf) < need:
                        if stop_ev.is_set():
                            return
                        try:
                            chunk = self_ref._pcm_q.get(timeout=2.0)
                        except queue.Empty:
                            if buf:
                                yield buf; buf = b""
                            return
                        if chunk == b"":
                            if buf:
                                yield buf
                            return
                        buf += chunk
                    yield buf[:need]
                    buf  = buf[need:]

            gen = pcm_gen()
            next(gen)

            dev = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=nch,
                sample_rate=rate,
                buffersize_msec=200
            )
            self_ref._dev = dev
            dev.start(gen)
            print(f"[LiveAnons] Playback başladı — {rate}Hz, {nch}ch")
            while dev.running:
                if stop_ev.is_set():
                    dev.stop()
                    break
                time.sleep(0.05)
            self_ref._dev = None
            print("[LiveAnons] Playback tamamlandı.")
        except Exception as e:
            import traceback
            print(f"[LiveAnons] Playback hatası: {e}")
            traceback.print_exc()
            self_ref._dev = None

_live_announce = _LiveAnnounce()

# ── WebRTC Canlı Anons ──────────────────────────────────────────────────────
_webrtc_state = {"pc": None, "receiver": None, "loop": None}
_webrtc_log   = []

def _wlog(msg):
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    entry = f"[{ts}] {msg}"
    _webrtc_log.append(entry)
    if len(_webrtc_log) > 80:
        _webrtc_log.pop(0)
    print(entry, flush=True)

def _ensure_webrtc_loop():
    """Asyncio event loop'u arka plan thread'inde başlat (yoksa) ve döndür."""
    import asyncio as _aio
    lp = _webrtc_state.get("loop")
    if lp is None or not lp.is_running():
        lp = _aio.new_event_loop()
        _webrtc_state["loop"] = lp
        threading.Thread(target=lp.run_forever, daemon=True).start()
    return lp

class _WebRTCReceiver:
    """aiortc ses track'inden PCM alır, miniaudio ile çalar. _LiveAnnounce ile aynı yapı."""

    def __init__(self):
        self._pcm_q   = queue.Queue(maxsize=50)
        self._stop_ev = threading.Event()
        self._dev     = None
        self._rate    = 48000

    def start_playback(self):
        threading.Thread(target=self._playback_worker, daemon=True).start()

    async def receive(self, track):
        """asyncio loop thread'inde — audio frame'leri s16 mono PCM'e çevirip kuyruğa koyar."""
        import av as _av
        resampler = None
        frame_count = 0
        playback_started = False
        _wlog(f"receive() başladı — track.kind={track.kind}")
        try:
            while not self._stop_ev.is_set():
                try:
                    frame = await track.recv()
                except Exception as _ex:
                    _wlog(f"receive() track.recv() hatası: {repr(_ex)}")
                    break
                frame_count += 1
                if resampler is None:
                    self._rate = frame.sample_rate
                    _wlog(f"İlk frame — sample_rate={frame.sample_rate} samples={frame.samples}")
                    resampler = _av.AudioResampler(format="s16", layout="mono",
                                                   rate=frame.sample_rate)
                if frame_count % 50 == 0:
                    _wlog(f"receive() {frame_count}. frame, queue={self._pcm_q.qsize()}")
                for f in resampler.resample(frame):
                    raw = bytes(f.planes[0])
                    if raw:
                        try:
                            self._pcm_q.put_nowait(raw)
                        except queue.Full:
                            pass
                        if not playback_started and frame_count >= 25:
                            playback_started = True
                            self.start_playback()
        except Exception as _e:
            _wlog(f"receive() genel hata: {repr(_e)}")
        _wlog(f"receive() bitti — toplam {frame_count} frame")

    def stop(self):
        self._stop_ev.set()
        try: self._pcm_q.put_nowait(b"")
        except: pass
        if self._dev:
            try: self._dev.stop()
            except: pass

    def _playback_worker(self):
        import miniaudio
        _wlog("_playback_worker başladı — ilk PCM bekleniyor (max 3sn)")
        stop_ev = self._stop_ev
        first_pcm = None
        while first_pcm is None:
            if stop_ev.is_set(): return
            try:
                first_pcm = self._pcm_q.get(timeout=3.0)
            except queue.Empty:
                _wlog("_playback_worker: 3sn içinde PCM gelmedi — çıkıyor")
                return
        if not first_pcm or stop_ev.is_set(): return
        _wlog(f"_playback_worker: ilk PCM alındı ({len(first_pcm)} bytes)")

        rate  = self._rate
        pcm_q = self._pcm_q

        def pcm_gen():
            buf      = first_pcm
            required = yield b""
            while True:
                if stop_ev.is_set(): return
                need = (required * 2) if required else 4096  # s16: 2 byte/frame
                # Engellenmez drain — kuyrukta ne varsa al, bekleme
                while len(buf) < need:
                    try:
                        chunk = pcm_q.get_nowait()
                    except queue.Empty:
                        break
                    if not chunk: return  # stop sinyali
                    buf += chunk
                if len(buf) >= need:
                    yield buf[:need]
                    buf = buf[need:]
                else:
                    # Veri yok — cızırtı yerine sessizlik üret
                    out = buf + bytes(need - len(buf))
                    buf = b""
                    yield out

        gen = pcm_gen()
        next(gen)
        dev = miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=rate,
            buffersize_msec=200
        )
        self._dev = dev
        dev.start(gen)
        _wlog(f"Playback başladı — {rate}Hz mono f32")
        while dev.running:
            if stop_ev.is_set():
                dev.stop()
                break
            time.sleep(0.05)
        self._dev = None
        _wlog("Playback tamamlandı.")

# Şu an çalan içeriği takip et (remote toggle için)
# {"type": "bell"|"ceremony"|"announce"|None, "key": "student"|"anthemOnly"|...}
_now_playing: dict = {"type": None, "key": None}

# index.html penceresi acik mi? Heartbeat ile takip edilir.
_window_last_heartbeat: float = 0.0   # epoch saniye
_HEARTBEAT_TIMEOUT: float = 60.0       # 60 sn heartbeat gelmezse pencere kapali sayilir

# server.py tarafindan calilan zilleri takip et — index.html pencere acilinca sorgular
# {"HH:MM": ["s_1", "t_2", ...]}  — _check_bells'te doldurulur, gun degisince sifirlanir
_fired_bells: dict = {}
_fired_bells_day: int = -1

def _is_window_open() -> bool:
    """index.html penceresi acik mi? (Son heartbeattan bu yana < 60 sn gecmisse evet)"""
    return (time.time() - _window_last_heartbeat) < _HEARTBEAT_TIMEOUT

def sse_broadcast(event: str, data: dict):
    """Tüm SSE istemcilerine komut yayınla."""
    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

def get_local_ip() -> str:
    """Yerel ağ IP adresini bul."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def get_mdns_hostname() -> str:
    """Bilgisayarın mDNS (.local) hostname'ini döndür.
    Örnek: OKULPC.local  — IP değişse bile aynı ağda çözümlenir."""
    try:
        return socket.gethostname() + ".local"
    except Exception:
        return ""

# ============================================================
# REGISTRY
# ============================================================
def _reg_key():
    import winreg
    return winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_ALL_ACCESS)

def is_startup_enabled():
    try:
        import winreg
        with _reg_key() as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except Exception:
        return False

def enable_startup():
    import winreg
    # Baslat.bat'i konsol penceresi acmadan calistiran VBS olustur
    # VBS dosyasini AppData altina yaz (Program Files'a yazma izni gerekmez)
    bat_path = os.path.join(SCRIPT_DIR, "Baslat.bat")
    vbs_path = os.path.join(DATA_DIR, "BaslatGizli.vbs")
    vbs_content = f'CreateObject("WScript.Shell").Run Chr(34) & "{bat_path}" & Chr(34), 0, False\n'
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(vbs_content)
    # Registry'e wscript.exe ile VBS'i kaydet
    wscript = os.path.join(os.environ.get("SystemRoot","C:\\Windows"),
                           "System32", "wscript.exe")
    cmd = f'"{wscript}" "{vbs_path}"'
    with _reg_key() as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)

def disable_startup():
    # 1. ONCE registry kaydini sil
    # (VBS dosyasi silinmeden once registry temizlenmeli, yoksa
    #  Windows bir sonraki acilista "komut dosyasi bulunamiyor" hatasi verir)
    try:
        import winreg
        with _reg_key() as k:
            winreg.DeleteValue(k, APP_NAME)
    except Exception:
        pass
    # 2. SONRA VBS dosyasini sil (her iki konumdan)
    for vbs_path in [
        os.path.join(DATA_DIR, "BaslatGizli.vbs"),    # yeni konum
        os.path.join(SCRIPT_DIR, "BaslatGizli.vbs"),  # eski konum (geriye dönük)
    ]:
        try:
            if os.path.exists(vbs_path):
                os.remove(vbs_path)
        except Exception:
            pass

# ============================================================
# NTP
# ============================================================
NTP_SERVERS = ["time.cloudflare.com","pool.ntp.org","time.google.com","tr.pool.ntp.org"]

def get_ntp_time_udp():
    for srv in NTP_SERVERS:
        try:
            c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            c.settimeout(2)
            c.sendto(b"\x1b" + 47*b"\x00", (srv, 123))
            d, _ = c.recvfrom(1024); c.close()
            if len(d) >= 48:
                return struct.unpack("!I", d[40:44])[0] - 2208988800, "udp"
        except Exception:
            pass
    return None, None

def get_ntp_time_w32tm():
    if platform.system() != "Windows": return None, None
    try:
        r = subprocess.run(["w32tm","/query","/status"],
                           capture_output=True, text=True, timeout=4)
        if r.returncode == 0: return int(time.time()), "w32tm"
    except Exception:
        pass
    return None, None

def get_ntp_time():
    ts, src = get_ntp_time_udp()
    return (ts, src) if ts else get_ntp_time_w32tm()


# ============================================================
# PYTHON ZİL ZAMANLAYICI — BellScheduler
# ============================================================
# Bu modül, zil çalma mantığını tamamen Python tarafında yürütür.
# WebView2 / tarayıcının arka plan throttling'inden etkilenmez.
# pygame.mixer ile ses dosyalarını doğrudan çalar.
# index.html'deki JavaScript zil sistemi de çalışmaya devam eder;
# Python scheduler ZİL ÇALACAK ANA SİSTEMDİR; JS ise yedek/görsel amaçlıdır.
# ============================================================

def _init_pygame_mixer():
    """pygame.mixer'ı başlatmayı dener. Yoksa yüklemeyi dener."""
    try:
        import pygame.mixer as _mx
        if not _mx.get_init():
            _mx.init(frequency=44100, size=-16, channels=2, buffer=512)
        return _mx
    except ImportError:
        try:
            import subprocess as _sp, sys as _sys
            _sp.run([_sys.executable, "-m", "pip", "install", "pygame", "--quiet"],
                    capture_output=True, timeout=60)
            import pygame.mixer as _mx
            _mx.init(frequency=44100, size=-16, channels=2, buffer=512)
            return _mx
        except Exception as _e:
            print(f"[BellScheduler] pygame kurulamadi: {_e}")
            return None

# Zamanlayici durum paylasimi — /api/scheduler-status endpoint icin
_scheduler_status: dict = {
    "enabled": False,
    "last_bell": None,
    "next_bell": None,
    "pygame_ok": False,
}

class BellScheduler:
    """
    Python tarafli zil zamanlayicisi.
    settings.json'i izler; her dakika zil vakti kontrolu yapar;
    pygame.mixer ile ses dosyalarini calar.
    """

    MELODY_MAP = {
        "1":  "1.mp3",
        "2":  "anons1.mp3",
        "3":  "anons2.mp3",
        None: "1.mp3",
        "":   "1.mp3",
    }

    DAY_KEYS = {
        0: "mondaySchedule",
        1: "tuesdaySchedule",
        2: "wednesdaySchedule",
        3: "thursdaySchedule",
        4: "fridaySchedule",
        5: "saturdaySchedule",
        6: "sundaySchedule",
    }

    def __init__(self):
        self._mx            = None
        self._settings      = {}
        self._last_played   = {}
        self._last_day      = -1
        self._playing       = False
        self._thread        = None
        self._stop_event    = threading.Event()
        self._settings_lock = threading.Lock()
        self._play_stop     = threading.Event()  # set() → mevcut sesi/anons zincirini durdur
        # Teneffüs müzik çalar — pygame.mixer.music modülü
        self._music_tracks  = []     # [path, ...] tam playlist
        self._music_index   = 0      # şu an çalınan/çalinacak parça
        self._music_paused  = False  # True → öğrenci zili için geçici pause
        self._music_stop    = threading.Event()
        self._music_thread  = None
        self._music_lock    = threading.Lock()
        # Manuel durdurma (Bütün Sesleri Durdur butonu) için konum kaydı
        self._paused_for_manual     = False
        self._paused_music_tracks   = []
        self._paused_music_index    = 0
        self._paused_music_pos_ms   = 0    # parça içi konum (ms)

    def reload_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._settings_lock:
                self._settings = data
        except Exception:
            pass

    def _get(self, key, default=None):
        with self._settings_lock:
            return self._settings.get(key, default)

    def _schedule_for_day(self, weekday: int) -> list:
        key  = self.DAY_KEYS.get(weekday, "mondaySchedule")
        rows = self._get(key)
        if rows is None:  # Anahtar hiç yok → Pazartesi'ye düş
            rows = self._get("mondaySchedule") or []
        return rows or []

    @staticmethod
    def _parse_time(t: str):
        try:
            h, m = t.split(":")
            return int(h), int(m)
        except Exception:
            return None

    def _is_holiday(self, now) -> bool:
        today = f"{now.year}-{now.month:02d}-{now.day:02d}"
        holidays = self._get("holidays") or []
        return any(h.get("date") == today for h in holidays)

    def _is_weekend_disabled(self, weekday: int) -> bool:
        is_we = weekday in (5, 6)
        return is_we and bool(self._get("disableWeekend", False))

    def _sound_path(self, bell_type: str) -> str:
        key_map = {
            "student": "studentMelody",
            "teacher": "teacherMelody",
            "break":   "breakMelody",
        }
        mel_val = self._get(key_map.get(bell_type, "studentMelody"), "1")
        if mel_val == "custom":
            custom_paths = self._get("customMelodyPath") or {}
            path = custom_paths.get(bell_type, "")
            if path and os.path.isfile(path):
                return path
            mel_val = "1"
        fname = self.MELODY_MAP.get(mel_val, "1.mp3")
        return os.path.join(SCRIPT_DIR, "sounds", "melodiler", fname)

    def _break_music_list(self, bell_row: dict) -> list:
        items = bell_row.get("breakMusicList") or []
        result = []
        for item in items:
            p = item.get("path", "") if isinstance(item, dict) else str(item)
            if not p:
                continue
            # Dosya doğrudan erişilebiliyorsa kullan
            if os.path.isfile(p):
                result.append(p)
                continue
            # Eski path Program Files altındaysa → DATA_DIR/muzikler altında ara
            fname = os.path.basename(p)
            alt = os.path.join(DATA_DIR, "muzikler", fname)
            if os.path.isfile(alt):
                result.append(alt)
        return result

    def _get_duration_ms(self, bell_type: str) -> int:
        dk_key = {"student": "studentMelodyDk", "teacher": "teacherMelodyDk",
                  "break": "breakMelodyDk"}.get(bell_type, "studentMelodyDk")
        sn_key = {"student": "studentMelodySn", "teacher": "teacherMelodySn",
                  "break": "breakMelodySn"}.get(bell_type, "studentMelodySn")
        try:
            dk = int(self._get(dk_key) or 0)
            sn = int(self._get(sn_key) or 0)
            total = (dk * 60 + sn) * 1000
            return total if total > 0 else 0
        except Exception:
            return 0

    # ── Teneffüs müzik yönetimi ────────────────────────────────────────────────

    # ── Teneffüs Müzik Sistemi (pygame.mixer.music) ─────────────────────────
    # pygame.mixer.music: tek kanal ama get_pos() ile ms cinsinden konum verir.
    # Bu sayede "Bütün Sesleri Durdur" sonrası kaldığı ms'den devam edilebilir.

    def _music_start(self, tracks: list, shuffle: bool = False, start_idx: int = 0, start_pos_ms: int = 0):
        """Teneffüs müziğini başlat.
        start_idx   : playlist içinde başlanacak parça (0-tabanlı)
        start_pos_ms: o parça içinde başlanacak konum (milisaniye)
        """
        if not self._mx or not tracks:
            return
        import random as _rnd
        # Önceki worker'ı durdur (stop event + music.stop)
        self._music_stop_now(save_pos=False)
        playlist = list(tracks)
        if shuffle:
            _rnd.shuffle(playlist)
        self._music_tracks = playlist
        self._music_index  = max(0, min(start_idx, len(playlist) - 1))
        self._music_paused = False
        self._music_stop.clear()

        _start_pos_ms = start_pos_ms  # ilk parça için başlangıç konumu

        def _worker():
            import pygame.mixer as _pmx
            idx = self._music_index
            while idx < len(self._music_tracks):
                if self._music_stop.is_set():
                    break
                path = self._music_tracks[idx]
                if not os.path.isfile(path):
                    print(f"[Muzik] Dosya yok, atlanıyor: {path}")
                    idx += 1
                    continue
                try:
                    _pmx.music.load(path)
                    pos_sn = (_start_pos_ms / 1000.0) if idx == self._music_index and _start_pos_ms > 0 else 0.0
                    _pmx.music.play(start=pos_sn)
                    self._music_index = idx
                    print(f"[Muzik] Çalıyor ({idx+1}/{len(self._music_tracks)}): {os.path.basename(path)}"
                          + (f" [{pos_sn:.1f}s'den]" if pos_sn > 0 else ""))
                    # Çalarken bekle — pause/stop destekli
                    while True:
                        if self._music_stop.is_set():
                            _pmx.music.stop()
                            return
                        if self._music_paused:
                            time.sleep(0.05)
                            continue
                        if not _pmx.music.get_busy():
                            break  # parça bitti, sıradakine geç
                        time.sleep(0.05)
                except Exception as e:
                    print(f"[Muzik] Calınamadı ({path}): {e}")
                idx += 1
            print("[Muzik] Playlist bitti.")

        self._music_thread = threading.Thread(target=_worker, daemon=True)
        self._music_thread.start()
        print(f"[Muzik] Başladı — {len(playlist)} parça, idx={self._music_index}, pos={start_pos_ms}ms")
        sse_broadcast("bell-status", {"ringing": True, "tip": "muzik"})

    def _music_get_pos_ms(self) -> int:
        """Şu an çalınan parçanın milisaniye cinsinden konumunu döndürür."""
        try:
            import pygame.mixer as _pmx
            pos = _pmx.music.get_pos()   # -1 ise çalmıyor
            return max(0, pos)
        except Exception:
            return 0

    def _music_pause(self):
        """Müziği geçici olarak duraklat — öğrenci zili için."""
        try:
            import pygame.mixer as _pmx
            if _pmx.music.get_busy() and not self._music_paused:
                _pmx.music.pause()
                self._music_paused = True
                print("[Muzik] Pause (öğrenci zili)")
        except Exception as e:
            print(f"[Muzik] Pause hatası: {e}")

    def _music_resume(self):
        """Öğrenci zili bittikten sonra müziği kaldığı yerden devam ettir."""
        if not self._music_paused:
            return
        try:
            import pygame.mixer as _pmx
            _pmx.music.unpause()
            self._music_paused = False
            print("[Muzik] Resume (öğrenci zili bitti)")
        except Exception as e:
            print(f"[Muzik] Resume hatası: {e}")

    def _music_stop_now(self, save_pos: bool = False):
        """Müziği tamamen durdur. save_pos=True ise konumu kaydet."""
        if save_pos:
            self._paused_music_pos_ms = self._music_get_pos_ms()
        self._music_stop.set()
        self._music_paused = False
        try:
            import pygame.mixer as _pmx
            _pmx.music.stop()
        except Exception:
            pass
        # Worker thread'in durmasını bekle (max 0.5s)
        if self._music_thread and self._music_thread.is_alive():
            self._music_thread.join(timeout=0.5)
        self._music_thread = None
        print("[Muzik] Durduruldu" + (" (konum kaydedildi)" if save_pos else ""))

    def _music_is_active(self) -> bool:
        """Müzik çalıyor veya pause'da mı?"""
        if self._music_paused:
            return True
        try:
            import pygame.mixer as _pmx
            return _pmx.music.get_busy()
        except Exception:
            return False

    # ── Ses çalma (zil melodileri) ────────────────────────────────────────────

    def _play_sound(self, path: str, duration_ms: int = 0, anons_path: str = None, on_finish=None):
        if not self._mx:
            return
        # Onceki sesi ve anons zincirini iptal et
        self._play_stop.set()
        time.sleep(0.1)
        self._play_stop.clear()

        def _worker(stop_ev: threading.Event):
            self._playing = True
            # Zil tipini dosya adından tahmin et
            fname = os.path.basename(path).lower()
            if "anons" in fname or "bitis" in fname:
                bell_tip = "anons"
            else:
                bell_tip = getattr(self, '_last_bell_type', 'breakTime')
            sse_broadcast("bell-status", {"ringing": True, "tip": bell_tip})
            try:
                snd = self._mx.Sound(path)
                ch  = snd.play()
                if ch is None:
                    return
                length_ms = int(snd.get_length() * 1000)
                wait_ms   = duration_ms if 0 < duration_ms < length_ms else length_ms
                wait_ms   = min(wait_ms, 60_000)
                end_time  = time.time() + wait_ms / 1000.0
                while time.time() < end_time and ch.get_busy():
                    if stop_ev.is_set():
                        ch.stop()
                        return
                    time.sleep(0.05)
                ch.stop()
                # Anons zinciri: melodi bittikten sonra anons dosyasini cal
                if anons_path and os.path.isfile(anons_path) and not stop_ev.is_set():
                    sse_broadcast("bell-status", {"ringing": True, "tip": "anons"})
                    for _ in range(16):   # 0.8 sn = 16 x 0.05
                        if stop_ev.is_set():
                            return
                        time.sleep(0.05)
                    try:
                        asnd = self._mx.Sound(anons_path)
                        ach  = asnd.play()
                        if ach:
                            alen = int(asnd.get_length() * 1000)
                            aend = time.time() + alen / 1000.0
                            while time.time() < aend and ach.get_busy():
                                if stop_ev.is_set():
                                    ach.stop()
                                    return
                                time.sleep(0.05)
                            ach.stop()
                    except Exception as ae:
                        print(f"[BellScheduler] Anons calınamadi ({anons_path}): {ae}")
            except Exception as e:
                print(f"[BellScheduler] Ses calınamadi ({path}): {e}")
            finally:
                self._playing = False
                if not stop_ev.is_set():
                    sse_broadcast("bell-status", {"ringing": False, "tip": ""})
                if on_finish and not stop_ev.is_set():
                    try:
                        on_finish()
                    except Exception as _cbe:
                        print(f"[BellScheduler] on_finish callback hatasi: {_cbe}")
        threading.Thread(target=_worker, args=(self._play_stop,), daemon=True).start()
    def _ko_ses_path(self, ses: str) -> str:
        """Kitap okuma giris melodisi dosya yolu (koGetSesURL mantigi)."""
        m = {
            "anons4": "anons4.mp3",
            "1":      "1.mp3",
            "2":      "anons1.mp3",
            "3":      "anons2.mp3",
        }
        fname = m.get(ses, "anons4.mp3")
        return os.path.join(SCRIPT_DIR, "sounds", "melodiler", fname)

    def _ko_cikis_ses_path(self, cikis_ses: str) -> str:
        """Kitap okuma cikis melodisi dosya yolu (koGetCikisSesURL mantigi)."""
        m = {
            "anons5": "1.mp3",
            "1":      "1.mp3",
            "2":      "anons1.mp3",
            "3":      "anons2.mp3",
        }
        fname = m.get(cikis_ses, "1.mp3")
        return os.path.join(SCRIPT_DIR, "sounds", "melodiler", fname)

    def _ko_kayit(self, ders_no, now) -> dict:
        """Bugun aktif kitap okuma kaydini dondurur (settings.json'dan)."""
        liste = self._get("kitapOkumaSaatleri") or []
        for k in liste:
            if (k.get("gun") == now.day
                    and k.get("ay")  == now.month
                    and k.get("yil") == now.year
                    and k.get("ders") == ders_no):
                return k
        return None

    def _check_bells(self, now):
        global _fired_bells, _fired_bells_day, _silent_mode, _bell_log
        weekday = now.weekday()
        # Gun degisince fired_bells sifirla
        if now.day != _fired_bells_day:
            _fired_bells_day = now.day
            _fired_bells.clear()
        if _silent_mode:
            return  # Sessiz Saatler aktif — zil çalma
        if self._is_holiday(now):
            return
        if self._is_weekend_disabled(weekday):
            return
        hhmm  = f"{now.hour:02d}:{now.minute:02d}"
        sched = self._schedule_for_day(weekday)

        def _mark_fired(key):
            if hhmm not in _fired_bells:
                _fired_bells[hhmm] = []
            if key not in _fired_bells[hhmm]:
                _fired_bells[hhmm].append(key)
        for row in sched:
            if not row.get("active", False):
                continue
            lesson = row.get("lesson", "")

            # ── Ogrenci girisi ──────────────────────────────────────────────
            if (row.get("student") == hhmm
                    and f"{hhmm}_s_{lesson}" not in self._last_played):
                self._last_played[f"{hhmm}_s_{lesson}"] = True
                _mark_fired(f"s_{lesson}")
                path = self._sound_path("student")
                dur  = self._get_duration_ms("student")
                if os.path.isfile(path):
                    anons_p = None
                    if self._get("enableStudentAnons", True):
                        anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", "anons1.mp3")
                    print(f"[BellScheduler] Ogrenci zili → {hhmm} (Ders {lesson})")
                    # Müzik çalıyorsa pause yap, zil+anons tamamen bittikten sonra resume et
                    muzik_aktif = self._music_is_active()
                    if muzik_aktif:
                        self._music_pause()
                    _resume_cb = self._music_resume if muzik_aktif else None
                    self._play_sound(path, dur, anons_path=anons_p, on_finish=_resume_cb)
                    _scheduler_status["last_bell"] = {"time": hhmm, "type": "student", "ts": time.time()}
                    with _bell_log_lock:
                        _bell_log.insert(0, {"time": hhmm, "type": "student", "lesson": lesson, "date": now.strftime("%Y-%m-%d"), "ts": time.time()})
                        if len(_bell_log) > 200: _bell_log.pop()

            # ── Ogretmen girisi ─────────────────────────────────────────────
            if (row.get("teacher") == hhmm
                    and f"{hhmm}_t_{lesson}" not in self._last_played):
                self._last_played[f"{hhmm}_t_{lesson}"] = True
                _mark_fired(f"t_{lesson}")
                # Öğretmen girişinde müzik varsa durdur
                if self._music_is_active():
                    self._music_stop_now()
                ko = self._ko_kayit(lesson, now)
                if ko:
                    # Kitap okuma saati: ozel giris melodisi + giris anonsu
                    ses      = ko.get("ses", "anons4")
                    giris_ms = ((ko.get("girisDk", 0) or 0) * 60 + (ko.get("girisSn", 0) or 0)) * 1000
                    mel_path = self._ko_ses_path(ses)
                    # girisAnons: custom ses ise anons6.mp3, diger sesler icin basla.mp3
                    if ko.get("girisAnons", True):
                        anons_fname = "anons6.mp3" if ses == "custom" else "basla.mp3"
                        anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", anons_fname)
                    else:
                        anons_p = None
                    if os.path.isfile(mel_path):
                        print(f"[BellScheduler] Kitap Okuma girisi → {hhmm} (Ders {lesson})")
                        self._play_sound(mel_path, giris_ms, anons_path=anons_p)
                        _scheduler_status["last_bell"] = {"time": hhmm, "type": "teacher", "ts": time.time()}
                        with _bell_log_lock:
                            _bell_log.insert(0, {"time": hhmm, "type": "teacher", "lesson": lesson, "date": now.strftime("%Y-%m-%d"), "ts": time.time()})
                            if len(_bell_log) > 200: _bell_log.pop()
                else:
                    # Normal ogretmen zili
                    path = self._sound_path("teacher")
                    dur  = self._get_duration_ms("teacher")
                    if os.path.isfile(path):
                        anons_p = None
                        if self._get("enableTeacherAnons", True):
                            anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", "anons2.mp3")
                        print(f"[BellScheduler] Ogretmen zili → {hhmm} (Ders {lesson})")
                        self._play_sound(path, dur, anons_path=anons_p)
                        _scheduler_status["last_bell"] = {"time": hhmm, "type": "teacher", "ts": time.time()}
                        with _bell_log_lock:
                            _bell_log.insert(0, {"time": hhmm, "type": "teacher", "lesson": lesson, "date": now.strftime("%Y-%m-%d"), "ts": time.time()})
                            if len(_bell_log) > 200: _bell_log.pop()

            # ── Teneffus / ders sonu ────────────────────────────────────────
            if (row.get("end") == hhmm
                    and f"{hhmm}_e_{lesson}" not in self._last_played):
                self._last_played[f"{hhmm}_e_{lesson}"] = True
                _mark_fired(f"e_{lesson}")
                # Kitap okuma saati varsa bitis.mp3 anonsu ekle, yoksa sadece melodi
                ko = self._ko_kayit(lesson, now)
                break_list = self._break_music_list(row)
                play_music = self._get("playMusicInBreak", False)
                # Her zaman teneffüs zili çal
                bpath = self._sound_path("break")
                dur   = self._get_duration_ms("break")
                anons_p = None
                if ko and ko.get("cikisAnons", True):
                    anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", "bitis.mp3")
                if os.path.isfile(bpath):
                    print(f"[BellScheduler] Teneffus zili → {hhmm} (Ders {lesson})")
                    # Müzik varsa: zil+anons tamamen bittikten sonra başlat (on_finish callback)
                    if break_list and play_music:
                        _tracks = list(self._break_music_list(row))
                        _shuf   = row.get("breakMusicShuffle", False)
                        def _on_zil_bitti(tracks=_tracks, shuf=_shuf):
                            print(f"[BellScheduler] Zil/anons bitti, teneffus muzigi basliyor → {len(tracks)} parça")
                            self._music_start(tracks, shuf)
                        self._play_sound(bpath, dur, anons_path=anons_p, on_finish=_on_zil_bitti)
                    else:
                        self._play_sound(bpath, dur, anons_path=anons_p)
                _scheduler_status["last_bell"] = {"time": hhmm, "type": "break", "ts": time.time()}
                with _bell_log_lock:
                    _bell_log.insert(0, {"time": hhmm, "type": "break", "lesson": lesson, "date": now.strftime("%Y-%m-%d"), "ts": time.time()})
                    if len(_bell_log) > 200: _bell_log.pop()

    def _update_next_bell(self, now):
        weekday = now.weekday()
        sched   = self._schedule_for_day(weekday)
        cur     = f"{now.hour:02d}:{now.minute:02d}"
        times   = []
        for row in sched:
            if not row.get("active", False):
                continue
            for t, typ in [(row.get("student"), "student"),
                           (row.get("teacher"), "teacher"),
                           (row.get("end"),     "break")]:
                if t and t > cur:
                    times.append((t, typ))
        times.sort()
        _scheduler_status["next_bell"] = {"time": times[0][0], "type": times[0][1]} if times else None

    def _run(self):
        _last_minute = -1
        _reload_ctr  = 0
        while not self._stop_event.is_set():
            try:
                import datetime as _dt
                now = _dt.datetime.now()
                if now.day != self._last_day:
                    self._last_day = now.day
                    self._last_played.clear()
                _reload_ctr += 1
                if _reload_ctr >= 30:
                    _reload_ctr = 0
                    self.reload_settings()
                if now.second <= 2 and now.minute != _last_minute:
                    _last_minute = now.minute
                    self._check_bells(now)
                    self._update_next_bell(now)
            except Exception as e:
                print(f"[BellScheduler] Dongu hatasi: {e}")
            time.sleep(1)

    def start(self):
        self._mx = _init_pygame_mixer()
        _scheduler_status["pygame_ok"] = self._mx is not None
        _scheduler_status["enabled"]   = True
        self.reload_settings()
        self._thread = threading.Thread(target=self._run, daemon=True, name="BellScheduler")
        self._thread.start()
        print(f"[BellScheduler] Basladi. pygame={'OK' if self._mx else 'HATA — ses calınamaz'}")

    def stop(self):
        self._stop_event.set()
        _scheduler_status["enabled"] = False

    def notify_settings_changed(self):
        self.reload_settings()

    def stop_remote(self):
        """Bütün Sesleri Durdur — hem zil/anons hem teneffüs müziğini durdurur.
        Müzik çalıyorsa parça index'i ve ms konumunu kaydeder; resume ile devam edilir."""
        muzik_aktif = self._music_is_active()
        self._paused_for_manual   = muzik_aktif
        self._paused_music_tracks = list(self._music_tracks)
        self._paused_music_index  = self._music_index
        # save_pos=True: _music_stop_now içinde get_pos() çağrılıp kaydedilir
        self._play_stop.set()            # zil/anons worker'ını durdur
        self._music_stop_now(save_pos=muzik_aktif)
        if not self._mx:
            return
        try:
            self._mx.stop()              # mixer kanallarını da temizle
        except Exception:
            pass
        self._playing = False
        if muzik_aktif:
            print(f"[Muzik] Manuel durdurma — idx={self._paused_music_index}, pos={self._paused_music_pos_ms}ms")

    def resume_music_external(self):
        """Kullanici 'Devam' bastığında teneffüs müziğini kaldığı parça+konumdan başlat."""
        if not self._paused_for_manual:
            print("[Muzik] Resume isteği ama paused_for_manual=False, yoksayılıyor")
            return
        self._paused_for_manual = False
        tracks  = self._paused_music_tracks
        idx     = self._paused_music_index
        pos_ms  = self._paused_music_pos_ms
        self._paused_music_tracks = []
        self._paused_music_pos_ms = 0
        if not tracks:
            print("[Muzik] Resume: kayıtlı playlist yok")
            return
        print(f"[Muzik] Manuel devam — idx={idx}, pos={pos_ms}ms, {len(tracks)} parça")
        self._music_start(tracks, start_idx=idx, start_pos_ms=pos_ms)

    def play_remote_bell(self, bell_type: str):
        """remote.html'den gelen zil komutu — direkt pygame ile cal."""
        if not self._mx:
            return
        # remote'dan "breakTime" gelir ama ic metodlar "break" bekler
        internal_type = "break" if bell_type == "breakTime" else bell_type
        path = self._sound_path(internal_type)
        dur  = self._get_duration_ms(internal_type)
        if not os.path.isfile(path):
            print(f"[BellScheduler] Remote zil — ses dosyasi bulunamadi: {path}")
            return
        anons_p = None
        if bell_type == "student":
            if self._get("enableStudentAnons", True):
                anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", "anons1.mp3")
        elif bell_type == "teacher":
            if self._get("enableTeacherAnons", True):
                anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", "anons2.mp3")
        elif bell_type == "breakTime":
            anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", "bitis.mp3")
        print(f"[BellScheduler] Remote zil → {bell_type} | ses: {path} | anons: {anons_p}")
        self._play_sound(path, dur, anons_path=anons_p)

    def play_remote_ceremony(self, cer_key: str):
        """remote.html'den gelen toren komutu — direkt pygame ile cal."""
        if not self._mx:
            return
        _sounds_dir = os.path.join(SCRIPT_DIR, "sounds", "marslar")
        # Toren ses haritasi (cerCustomURLs olmadan sadece varsayilan dosyalar)
        _cer_map = {
            "anthemOnly":  "istiklal.wav",
            "anthem1min":  "saygidurusu.wav",
            "anthem2min":  ["saygi.wav", "istiklal.wav"],   # zincir
            "silence1min": "saygidurusu.wav",
            "silence2min": "saygi.wav",
            "silenceOnly": "saygidurusu.wav",
            "emergency":   "siren.wav",
        }
        entry = _cer_map.get(cer_key)
        if not entry:
            return
        if isinstance(entry, list):
            # Zincir: ilk biter, ikinci basar
            p1 = os.path.join(_sounds_dir, entry[0])
            p2 = os.path.join(_sounds_dir, entry[1])
            if os.path.isfile(p1):
                print(f"[BellScheduler] Remote toren → {cer_key} (zincir)")
                self._play_sound(p1, 0, anons_path=p2 if os.path.isfile(p2) else None)
        else:
            p = os.path.join(_sounds_dir, entry)
            if os.path.isfile(p):
                print(f"[BellScheduler] Remote toren → {cer_key}")
                self._play_sound(p, 0)

    def play_ko_local(self, tip: str, ses: str, limit_ms: int, anons: bool):
        """Kitap okuma giris/cikis sesini yerel olarak cal (pencere kapali olsa da calisir)."""
        if not self._mx:
            return
        if tip == "giris":
            path   = self._ko_ses_path(ses)
            anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", "basla.mp3") if anons else None
        else:
            path   = self._ko_cikis_ses_path(ses)
            anons_p = os.path.join(SCRIPT_DIR, "sounds", "melodiler", "bitis.mp3") if anons else None
        if not os.path.isfile(path):
            print(f"[BellScheduler] KO ses bulunamadi: {path}")
            return
        if anons_p and not os.path.isfile(anons_p):
            anons_p = None
        self._play_sound(path, limit_ms, anons_path=anons_p)

    def play_announce_file(self, fpath: str):
        """Cep telefonundan gelen anons dosyasini pygame ile dogrudan cal.
        index.html kapali oldugunda kullanilir.
        pygame.mixer.music kullanilir: WAV, MP3, OGG destekler (ffmpeg gerekmez)."""
        if not self._mx:
            print("[BellScheduler] Anons: pygame hazir degil, ses calinmiyor.")
            return
        if not os.path.isfile(fpath):
            print(f"[BellScheduler] Anons dosyasi bulunamadi: {fpath}")
            return

        def _worker():
            self._playing = True
            try:
                # Anons öncesi müzik dahil her şeyi durdur
                self._music_stop_now(save_pos=False)
                self._play_stop.set()
                time.sleep(0.1)
                self._play_stop.clear()
                stop_ev = self._play_stop

                import pygame
                # pygame.mixer.music: WAV/MP3/OGG destekler — ek kutüphane gerekmez
                pygame.mixer.music.load(fpath)
                pygame.mixer.music.set_volume(1.0)
                pygame.mixer.music.play()
                print(f"[BellScheduler] Anons oynatiliyor (music): {os.path.basename(fpath)}")
                # Bitmesini bekle
                while pygame.mixer.music.get_busy():
                    if stop_ev.is_set():
                        pygame.mixer.music.stop()
                        return
                    time.sleep(0.05)
                print(f"[BellScheduler] Anons tamamlandi: {os.path.basename(fpath)}")
            except Exception as e:
                print(f"[BellScheduler] Anons calinirken hata ({fpath}): {e}")
                # music basarisiz olursa Sound ile dene (sadece WAV/MP3)
                try:
                    snd = self._mx.Sound(fpath)
                    ch  = snd.play()
                    if ch:
                        end_t = time.time() + snd.get_length()
                        while time.time() < end_t and ch.get_busy():
                            time.sleep(0.05)
                        ch.stop()
                except Exception as e2:
                    print(f"[BellScheduler] Sound fallback da basarisiz: {e2}")
            finally:
                self._playing = False

        threading.Thread(target=_worker, daemon=True).start()

# Global scheduler ornegi
_bell_scheduler = BellScheduler()

# ============================================================
# HTTP SUNUCU
# ============================================================
class ZilHandler(http.server.SimpleHTTPRequestHandler):
    # Statik dosyalar için sabit dizin — os.chdir'e bağımlı olmaz
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def log_message(self, *a): pass

    def _check_token(self):
        """API isteklerinde güvenlik token'ını doğrula.
        Hem X-Api-Token header'ından hem de URL query string'inden (?token=...) kabul eder.
        <audio> gibi tarayıcı elementleri header gönderemediğinden query string de desteklenir.
        """
        # 1. Header'dan kontrol et
        token = self.headers.get("X-Api-Token", "")
        if token == API_TOKEN:
            return True
        # 2. Query string'den kontrol et (?token=...)
        import urllib.parse
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        token_qs = qs.get("token", [""])[0]
        return token_qs == API_TOKEN

    def do_OPTIONS(self):
        p = self.path.split("?")[0]
        # Uzaktan erişim endpoint'leri PIN ile korunur, OPTIONS için token gerekmez
        if not p.startswith("/api/remote/") and not self._check_token():
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        for h,v in [("Access-Control-Allow-Origin","*"),
                    ("Access-Control-Allow-Methods","GET, POST, OPTIONS"),
                    ("Access-Control-Allow-Headers","Content-Type, X-Api-Token, X-Remote-Pin")]:
            self.send_header(h, v)
        self.end_headers()

    def do_GET(self):
        p = self.path.split('?')[0]  # query string'i ayır
        # Uzaktan erişim endpoint'leri kendi PIN kontrolünü yapar
        if p.startswith("/api/") and not p.startswith("/api/remote/"):
            if not self._check_token():
                self._json({"error": "Yetkisiz erişim"}, 403); return
        if   p == "/api/load-settings":  self._file(SETTINGS_FILE, {"found":False})
        elif p == "/api/script-dir":        self._json({"dir": SCRIPT_DIR})
        elif p == "/api/load-profiles":  self._file(PROFILES_FILE, [])
        elif p == "/api/startup-status": self._json({"enabled": is_startup_enabled()})
        elif p == "/api/ntp-time":
            lt = time.time(); nt, src = get_ntp_time()
            if nt: self._json({"success":True,"ntp":nt,"local":lt,"offset":nt-lt,"source":src})
            else:  self._json({"success":False,"local":lt,"offset":0,"source":"none"})
        elif p == "/api/serve-audio":
            # Yerel ses dosyasını stream et: /api/serve-audio?path=C:\...&token=...
            import urllib.parse
            qs = urllib.parse.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
            fpath = qs.get("path", [""])[0]
            # Dosya doğrudan bulunamazsa DATA_DIR/muzikler altında ara
            if fpath and not os.path.isfile(fpath):
                alt = os.path.join(DATA_DIR, "muzikler", os.path.basename(fpath))
                if os.path.isfile(alt):
                    fpath = alt
            if not fpath or not os.path.isfile(fpath):
                self.send_response(404); self.send_header("Content-type","application/json")
                self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
                self.wfile.write(json.dumps({"error":"Dosya bulunamadi"}).encode()); return
            ext  = os.path.splitext(fpath)[1].lower().lstrip(".")
            mime = {"mp3":"audio/mpeg","wav":"audio/wav","ogg":"audio/ogg",
                    "flac":"audio/flac","aac":"audio/aac","m4a":"audio/mp4"}.get(ext,"audio/mpeg")
            try:
                fsize = os.path.getsize(fpath)
                self.send_response(200); self.send_header("Content-type", mime)
                self.send_header("Content-Length", str(fsize))
                self.send_header("Accept-Ranges","bytes")
                self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
                with open(fpath,"rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
            except Exception: pass
            return
        elif p == "/api/heartbeat":
            global _window_last_heartbeat
            _window_last_heartbeat = time.time()
            self._json({"ok": True})

        elif p == "/api/scheduler-status":
            self._json(_scheduler_status)

        elif p == "/api/fired-bells":
            # index.html pencere acildiginda bu dakikada server.py zil caldiysa ogrensin
            # Donus: {"minute": "HH:MM", "keys": ["s_1", "t_2", ...]} veya {"minute": null}
            import datetime as _dt2
            _now2 = _dt2.datetime.now()
            _hhmm2 = f"{_now2.hour:02d}:{_now2.minute:02d}"
            _keys = _fired_bells.get(_hhmm2, [])
            self._json({"minute": _hhmm2 if _keys else None, "keys": _keys})
        elif p == "/api/resmi-tatiller":
            # Türkiye resmi tatillerini hesapla (internet gerekmez)
            import datetime as _dt3, urllib.parse as _up3
            _qs3 = _up3.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
            _yil = int(_qs3.get("yil", [_dt3.date.today().year])[0])

            def _hijri_to_greg(hy, hm, hd):
                """Hijri takviminden Gregorian tarihe dönüşüm."""
                _n = hd + round(29.5001*(hm-1)) + (hy-1)*354
                _n += round((3+11*hy)/30) + 1948438
                _l = _n + 68569
                _n2 = (4*_l)//146097
                _l = _l - (146097*_n2+3)//4
                _i = (4000*(_l+1))//1461001
                _l = _l - (1461*_i)//4 + 31
                _j = (80*_l)//2447
                _d = _l - (2447*_j)//80
                _l = _j//11
                _m = _j + 2 - 12*_l
                _y = 100*(_n2-49) + _i + _l
                return _dt3.date(_y, _m, _d)

            def _dini_tatiller(yil):
                """Verilen Gregorian yıl için dini bayram günlerini hesapla."""
                sonuc = []
                td = _dt3.timedelta
                for hy in range(yil - 579, yil - 576):
                    r = _hijri_to_greg(hy, 10, 1)  # 1 Şevval - Ramazan Bayramı
                    if r.year == yil:
                        sonuc += [
                            (str(r - td(days=1)), "Ramazan Bayramı Arefesi"),
                            (str(r),              "Ramazan Bayramı 1. Günü"),
                            (str(r + td(days=1)), "Ramazan Bayramı 2. Günü"),
                            (str(r + td(days=2)), "Ramazan Bayramı 3. Günü"),
                        ]
                    k = _hijri_to_greg(hy, 12, 10)  # 10 Zilhicce - Kurban Bayramı
                    if k.year == yil:
                        sonuc += [
                            (str(k - td(days=1)), "Kurban Bayramı Arefesi"),
                            (str(k),              "Kurban Bayramı 1. Günü"),
                            (str(k + td(days=1)), "Kurban Bayramı 2. Günü"),
                            (str(k + td(days=2)), "Kurban Bayramı 3. Günü"),
                            (str(k + td(days=3)), "Kurban Bayramı 4. Günü"),
                        ]
                return sonuc

            _sabit = [
                (f"{_yil}-01-01", "Yılbaşı"),
                (f"{_yil}-04-23", "Ulusal Egemenlik ve Çocuk Bayramı"),
                (f"{_yil}-05-01", "Emek ve Dayanışma Günü"),
                (f"{_yil}-05-19", "Atatürkü Anma, Gençlik ve Spor Bayramı"),
                (f"{_yil}-07-15", "Demokrasi ve Millî Birlik Günü"),
                (f"{_yil}-08-30", "Zafer Bayramı"),
                (f"{_yil}-10-29", "Cumhuriyet Bayramı"),
            ]
            _tum = _sabit + _dini_tatiller(_yil)
            _sonuc = []
            for _tarih, _ad in _tum:
                try:
                    _dt3.date.fromisoformat(_tarih)
                    _sonuc.append({"date": _tarih, "name": _ad})
                except Exception:
                    pass
            _sonuc.sort(key=lambda x: x["date"])
            self._json({"holidays": _sonuc, "year": _yil})

        elif p == "/api/check-audio-file":
            # Dosya varlığını kontrol et
            import urllib.parse
            qs = urllib.parse.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
            fpath = qs.get("path", [""])[0]
            exists = bool(fpath and os.path.isfile(fpath))
            if not exists and fpath:
                alt = os.path.join(DATA_DIR, "muzikler", os.path.basename(fpath))
                exists = os.path.isfile(alt)
            self._json({"exists": exists})

        # ------ UZAKTAN ERİŞİM GET ------
        elif p == "/api/remote/ping":
            pin = self.headers.get("X-Remote-Pin", "")
            import urllib.parse
            qs  = urllib.parse.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
            if not pin: pin = qs.get("pin", [""])[0]
            if not check_remote_pin(pin):
                self._json({"ok": False, "error": "Geçersiz PIN"}, 403); return
            self._json({"ok": True, "server": APP_NAME, "time": time.time()})

        elif p == "/api/remote/discover":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"app": "NeneOkulZil", "version": "2.0"}).encode())
            return

        elif p == "/api/remote/zil-bul-indir":
            zb = os.path.join(SCRIPT_DIR, "zil-bul.html")
            if not os.path.exists(zb):
                self.send_response(404); self.end_headers(); return
            fsize = os.path.getsize(zb)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=zil-bul.html")
            self.send_header("Content-Length", str(fsize))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(zb, "rb") as f:
                self.wfile.write(f.read())
            return

        elif p == "/api/remote/status":
            # Şu an çalan içeriği döner — remote toggle için kullanılır
            pin = self.headers.get("X-Remote-Pin", "")
            import urllib.parse
            qs  = urllib.parse.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
            if not pin: pin = qs.get("pin", [""])[0]
            if not check_remote_pin(pin):
                self._json({"ok": False, "error": "Geçersiz PIN"}, 403); return
            self._json({"ok": True, "playing": _now_playing.copy()})

        elif p == "/api/remote/today-schedule":
            pin = self.headers.get("X-Remote-Pin", "")
            import urllib.parse as _ul2
            qs2 = _ul2.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
            if not pin: pin = qs2.get("pin", [""])[0]
            if not check_remote_pin(pin):
                self._json({"ok": False, "error": "Geçersiz PIN"}, 403); return
            import datetime as _dt3
            _now3   = _dt3.datetime.now()
            _today3 = f"{_now3.year}-{_now3.month:02d}-{_now3.day:02d}"
            _sched3 = _bell_scheduler._schedule_for_day(_now3.weekday())
            _hols3  = _bell_scheduler._get("holidays") or []
            _is_hol = any(h.get("date") == _today3 for h in _hols3)
            _rows3  = []
            for row in _sched3:
                if row.get("active", False):
                    _rows3.append({
                        "student": row.get("student"),
                        "teacher": row.get("teacher"),
                        "end":     row.get("end"),
                    })
            self._json({
                "ok":        True,
                "today":     _today3,
                "is_holiday": _is_hol,
                "schedule":  _rows3,
                "next_bell": _scheduler_status.get("next_bell"),
            })

        elif p == "/api/remote/notify-stopped":
            # index.html ses bittiğinde API token ile çağırır — _now_playing'i sıfırlar
            # Not: /api/remote/ prefix altında ama bu endpoint token korumalıdır
            if not self._check_token():
                self._json({"ok": False, "error": "Yetkisiz"}, 403); return
            _now_playing["type"] = None
            _now_playing["key"]  = None
            self._json({"ok": True})

        elif p == "/api/remote/local-ip":
            self._json({"ip": get_local_ip(), "port": PORT, "https_port": HTTPS_PORT if _HTTPS_RUNNING else None, "mdns": get_mdns_hostname(), "hostname": socket.gethostname(), "ntfyTopic": _NTFY_TOPIC or ""})

        elif p == "/api/remote/install-cert":
            # Sertifikayı .crt olarak indir — telefona CA sertifikası yüklemek için
            if not os.path.exists(SSL_CERT):
                self.send_response(404); self.end_headers(); return
            try:
                fsize = os.path.getsize(SSL_CERT)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-x509-ca-cert")
                self.send_header("Content-Disposition", "attachment; filename=zil_cert.crt")
                self.send_header("Content-Length", str(fsize))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(SSL_CERT, "rb") as f:
                    self.wfile.write(f.read())
            except Exception:
                self.send_response(500); self.end_headers()

        elif p == "/api/remote/sse":
            import urllib.parse
            qs  = urllib.parse.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
            pin = self.headers.get("X-Remote-Pin", "") or qs.get("pin", [""])[0]
            if not check_remote_pin(pin):
                self._json({"error": "Geçersiz PIN"}, 403); return

            q = queue.Queue(maxsize=50)
            with _sse_lock:
                _sse_clients.append(q)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()
            except Exception:
                with _sse_lock:
                    if q in _sse_clients: _sse_clients.remove(q)
                return
            while not _shutdown_event.is_set():
                try:
                    msg = q.get(timeout=25)
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except queue.Empty:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                except Exception:
                    break
            with _sse_lock:
                if q in _sse_clients: _sse_clients.remove(q)

        elif p == "/api/remote/announce-audio":
            import urllib.parse
            qs    = urllib.parse.parse_qs(self.path.split("?",1)[1] if "?" in self.path else "")
            fname = qs.get("file", [""])[0]
            pin   = qs.get("pin",  [""])[0]
            if not check_remote_pin(pin):
                self._json({"error": "Geçersiz PIN"}, 403); return
            fpath = os.path.join(ANNOUNCE_DIR, os.path.basename(fname))
            if not os.path.isfile(fpath):
                self.send_response(404); self.end_headers(); return
            ext  = os.path.splitext(fpath)[1].lower().lstrip(".")
            mime = {"mp3":"audio/mpeg","wav":"audio/wav","ogg":"audio/ogg",
                    "webm":"audio/webm","m4a":"audio/mp4"}.get(ext,"audio/octet-stream")
            fsize = os.path.getsize(fpath)
            self.send_response(200)
            self.send_header("Content-type", mime)
            self.send_header("Content-Length", str(fsize))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(fpath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)

        else:
            # Statik dosyalar için path'ten token'ı temizle
            self.path = p
            # remote.html ve sw.js her zaman taze gelsin — tarayıcı cache'i engelle
            if p in ('/remote.html', '/sw.js'):
                _fpath = os.path.join(SCRIPT_DIR, p.lstrip('/'))
                if os.path.exists(_fpath):
                    with open(_fpath, 'rb') as _fh:
                        _fc = _fh.read()
                    _ct = 'text/html; charset=utf-8' if p.endswith('.html') else 'application/javascript'
                    self.send_response(200)
                    self.send_header('Content-Type', _ct)
                    self.send_header('Content-Length', str(len(_fc)))
                    self.send_header('Cache-Control', 'no-store, must-revalidate')
                    self.send_header('Pragma', 'no-cache')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(_fc)
                    return
            super().do_GET()

    def do_POST(self):
        p = self.path.split('?')[0]  # query string'i temizle
        # Uzaktan erişim endpoint'leri kendi PIN kontrolünü yapar
        if not p.startswith("/api/remote/"):
            if not self._check_token():
                self._json({"error": "Yetkisiz erişim"}, 403); return
        if   p == "/api/save-settings":   self._save(SETTINGS_FILE)
        elif p == "/api/save-profiles":   self._save(PROFILES_FILE)
        elif p == "/api/upload-music":
            try:
                import urllib.parse as _ulp
                raw_name = self.headers.get("X-File-Name", "muzik.mp3")
                try:
                    raw_name = _ulp.unquote(raw_name)
                except Exception:
                    pass
                fname = os.path.basename(raw_name.replace("\\", "/"))
                if not fname:
                    fname = "muzik.mp3"
                muzikler_dir = os.path.join(SCRIPT_DIR, "sounds", "muzikler")
                os.makedirs(muzikler_dir, exist_ok=True)
                dest = os.path.join(muzikler_dir, fname)
                clen = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(clen)
                with open(dest, "wb") as f:
                    f.write(data)
                print(f"[upload-music] Kaydedildi: {dest} ({len(data)} byte)")
                self._json({"success": True, "path": dest, "name": fname})
            except Exception as e:
                print(f"[upload-music] HATA: {e}")
                self._json({"success": False, "error": str(e)}, 500)
            return
        elif p == "/api/bell-log":
            with _bell_log_lock:
                log_snapshot = _bell_log[:50]
            self._json({"log": log_snapshot})
            return
        elif p == "/api/save-custom-melody":
            # Özel melodi dosyasını diske kaydet; Python zil çalarken kullanabilsin
            try:
                mel_type = self.path.split("type=")[-1].split("&")[0] if "type=" in self.path else ""
                if mel_type not in ("student", "teacher", "break"):
                    self._json({"success": False, "error": "Geçersiz tür"}, 400)
                    return
                ext = self.headers.get("X-File-Ext", "mp3").lstrip(".")
                ext = ext if ext in ("mp3", "wav", "ogg", "m4a", "aac") else "mp3"
                clen = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(clen)
                dest = os.path.join(DATA_DIR, f"custom_melody_{mel_type}.{ext}")
                with open(dest, "wb") as f:
                    f.write(data)
                # settings.json'daki customMelodyPath'i güncelle
                try:
                    with open(SETTINGS_FILE, "r", encoding="utf-8") as sf:
                        s = json.load(sf)
                except Exception:
                    s = {}
                if "customMelodyPath" not in s or not isinstance(s["customMelodyPath"], dict):
                    s["customMelodyPath"] = {}
                s["customMelodyPath"][mel_type] = dest
                with open(SETTINGS_FILE, "w", encoding="utf-8") as sf:
                    json.dump(s, sf, ensure_ascii=False, indent=2)
                _bell_scheduler.notify_settings_changed()
                self._json({"success": True, "path": dest})
            except Exception as e:
                self._json({"success": False, "error": str(e)}, 500)
            return
        elif p == "/api/delete-custom-melody":
            # Özel melodiyi diskten sil
            try:
                mel_type = self.path.split("type=")[-1].split("&")[0] if "type=" in self.path else ""
                if mel_type not in ("student", "teacher", "break"):
                    self._json({"success": False, "error": "Geçersiz tür"}, 400)
                    return
                try:
                    with open(SETTINGS_FILE, "r", encoding="utf-8") as sf:
                        s = json.load(sf)
                except Exception:
                    s = {}
                old_path = (s.get("customMelodyPath") or {}).get(mel_type, "")
                if old_path and os.path.isfile(old_path):
                    os.remove(old_path)
                if "customMelodyPath" in s and isinstance(s["customMelodyPath"], dict):
                    s["customMelodyPath"].pop(mel_type, None)
                with open(SETTINGS_FILE, "w", encoding="utf-8") as sf:
                    json.dump(s, sf, ensure_ascii=False, indent=2)
                _bell_scheduler.notify_settings_changed()
                self._json({"success": True})
            except Exception as e:
                self._json({"success": False, "error": str(e)}, 500)
            return
        elif p == "/api/set-silent-mode":
            global _silent_mode
            import json as _json_sm
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            req  = _json_sm.loads(body) if body else {}
            _silent_mode = bool(req.get('active', False))
            self._json({"ok": True, "silent_mode": _silent_mode})
        elif p == "/api/play-bell-local":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req2 = json.loads(body) if body else {}
            bell_type = req2.get("type", "student")
            if bell_type not in ("student", "teacher", "breakTime"):
                self._json({"ok": False, "error": "Geçersiz zil tipi"}); return
            _bell_scheduler.play_remote_bell(bell_type)
            _now_playing["type"] = "bell"
            _now_playing["key"]  = bell_type
            self._json({"ok": True})
        elif p == "/api/play-ceremony-local":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req2 = json.loads(body) if body else {}
            cer_key = req2.get("key", "")
            if not cer_key:
                self._json({"ok": False, "error": "key eksik"}); return
            _bell_scheduler.play_remote_ceremony(cer_key)
            _now_playing["type"] = "ceremony"
            _now_playing["key"]  = cer_key
            self._json({"ok": True})
        elif p == "/api/play-melody-local":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req2 = json.loads(body) if body else {}
            bell_type = req2.get("type", "student")
            if bell_type not in ("student", "teacher", "breakTime"):
                self._json({"ok": False, "error": "Geçersiz tip"}); return
            _bell_scheduler.play_remote_bell(bell_type)
            _now_playing["type"] = "bell"
            _now_playing["key"]  = bell_type
            self._json({"ok": True})
        elif p == "/api/play-ko-local":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req2 = json.loads(body) if body else {}
            tip      = req2.get("tip", "giris")
            ses      = req2.get("ses", "anons4")
            limit_ms = int(req2.get("limitMs", 0))
            anons    = bool(req2.get("anons", False))
            _bell_scheduler.play_ko_local(tip, ses, limit_ms, anons)
            _now_playing["type"] = "ko"
            _now_playing["key"]  = tip
            self._json({"ok": True})
        elif p == "/api/stop-all-local":
            # Yerel "Bütün Sesleri Durdur" — pencere açıkken de çalışır
            _bell_scheduler.stop_remote()
            _live_announce.stop_immediate()
            _now_playing["type"] = None
            _now_playing["key"]  = None
            self._json({"ok": True})
        elif p == "/api/resume-music-local":
            # Yerel "Kaldığı Yerden Devam" — sadece teneffüs müziği devam ettirilir
            _bell_scheduler.resume_music_external()
            self._json({"ok": True})
        elif p == "/api/startup-enable":  self._try(enable_startup)
        elif p == "/api/startup-disable": self._try(disable_startup)
        elif p == "/api/wake-enable":     self._try(register_wake_task)
        elif p == "/api/wake-disable":    self._try(remove_wake_task)
        elif p == "/api/hide-window":
            self._json({"success":True})
            def _hide():
                time.sleep(.1)
                if _webview_window:
                    try: _webview_window.hide()
                    except Exception: pass
            threading.Thread(target=_hide, daemon=True).start()
        elif p == "/api/shutdown":
            self._json({"success":True})
            threading.Thread(target=lambda:(time.sleep(.5), _shutdown_event.set()),
                             daemon=True).start()
        elif p == "/api/shutdown-os":
            try:
                subprocess.Popen(["shutdown","/s","/t","0"] if platform.system()=="Windows"
                                 else ["shutdown","-h","now"])
                self._json({"success":True})
            except Exception as e: self._json({"success":False,"error":str(e)},500)
        elif p == "/api/shutdown-os-cancel":
            try:
                subprocess.Popen(["shutdown","/a"] if platform.system()=="Windows"
                                 else ["shutdown","-c"])
                self._json({"success":True})
            except Exception as e: self._json({"success":False,"error":str(e)},500)
        elif p == "/api/open-file-dialog":
            try:
                import json as _json
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                req  = _json.loads(body) if body else {}
                # Varsayılan klasörü belirle: sounds\teneffus > sounds > SCRIPT_DIR
                sounds_teneffus = os.path.join(SCRIPT_DIR, 'sounds', 'teneffus')
                sounds_dir      = os.path.join(SCRIPT_DIR, 'sounds')
                os.makedirs(sounds_teneffus, exist_ok=True)
                default_dir = req.get('defaultDir', sounds_teneffus)
                if not os.path.isdir(default_dir):
                    default_dir = sounds_dir if os.path.isdir(sounds_dir) else SCRIPT_DIR
                multiple = req.get('multiple', False)
                if multiple:
                    # Çoklu seçim: sadece yolları döndür (base64 yok — dosyalar büyük olabilir)
                    fpaths = _win_open_file_dialog(default_dir, multiple=True)
                    if fpaths:
                        files = []
                        for fp in fpaths:
                            if os.path.isfile(fp):
                                files.append({'path': fp, 'name': os.path.basename(fp)})
                        self._json({'success': True, 'files': files})
                    else:
                        self._json({'success': False, 'cancelled': True})
                else:
                    # Tekli seçim: eski davranış — base64 + yol döndür
                    fpath = _win_open_file_dialog(default_dir, multiple=False)
                    if fpath and os.path.isfile(fpath):
                        import base64
                        with open(fpath, 'rb') as f:
                            data = base64.b64encode(f.read()).decode('ascii')
                        ext  = os.path.splitext(fpath)[1].lower()
                        mime = {'mp3':'audio/mpeg','wav':'audio/wav','ogg':'audio/ogg',
                                'flac':'audio/flac','aac':'audio/aac','m4a':'audio/mp4'
                               }.get(ext.lstrip('.'), 'audio/mpeg')
                        self._json({'success': True, 'name': os.path.basename(fpath),
                                    'path': fpath, 'mime': mime, 'data': data})
                    else:
                        self._json({'success': False, 'cancelled': True})
            except Exception as e:
                import traceback
                print(f"[open-file-dialog] HATA: {e}\n{traceback.format_exc()}")
                self._json({'success': False, 'error': str(e)}, 500)

        # ------ UZAKTAN ERİŞİM POST ------
        # Bu endpoint'ler API_TOKEN değil PIN ile korunur (uzak cihazlar token bilmez)
        elif p in ("/api/remote/play-bell", "/api/remote/play-ceremony",
                   "/api/remote/stop-all", "/api/remote/announce",
                   "/api/remote/stream-start", "/api/remote/stream-chunk",
                   "/api/remote/stream-end",
                   "/api/remote/webrtc-offer", "/api/remote/webrtc-stop", "/api/remote/webrtc-debug",
                   "/api/remote/set-pin",
                   "/api/remote/fix-firefox",
                   "/api/remote/toggle-holiday"):
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                req  = json.loads(body) if body else {}
            except Exception:
                req  = {}
            pin = self.headers.get("X-Remote-Pin", "") or req.get("pin", "")
            if not check_remote_pin(pin):
                self._json({"ok": False, "error": "Geçersiz PIN"}, 403); return

            if p == "/api/remote/play-bell":
                bell_type = req.get("type", "student")  # student | teacher | breakTime
                if bell_type not in ("student", "teacher", "breakTime"):
                    self._json({"ok": False, "error": "Geçersiz zil tipi"}); return
                sse_broadcast("play-bell", {"type": bell_type})
                _now_playing["type"] = "bell"
                _now_playing["key"]  = bell_type
                # Pencere kapali ise server.py direkt calar
                if not _is_window_open():
                    _bell_scheduler.play_remote_bell(bell_type)
                self._json({"ok": True})

            elif p == "/api/remote/play-ceremony":
                cer_key = req.get("key", "")
                valid_keys = ("anthemOnly","anthem1min","anthem2min","silence1min",
                              "silence2min","silenceOnly","emergency")
                if cer_key not in valid_keys:
                    self._json({"ok": False, "error": "Geçersiz tören anahtarı"}); return
                sse_broadcast("play-ceremony", {"key": cer_key})
                _now_playing["type"] = "ceremony"
                _now_playing["key"]  = cer_key
                # Pencere kapali ise server.py direkt calar
                if not _is_window_open():
                    _bell_scheduler.play_remote_ceremony(cer_key)
                self._json({"ok": True})

            elif p == "/api/remote/stop-all":
                sse_broadcast("stop-all", {})
                _now_playing["type"] = None
                _now_playing["key"]  = None
                # Pencere kapali ise server.py pygame'i ve live anons'u durdurur
                if not _is_window_open():
                    _bell_scheduler.stop_remote()
                    _live_announce.stop_immediate()
                self._json({"ok": True})

            elif p == "/api/remote/announce":
                # Ses verisi: base64 olarak gelmeli  { "audio": "data:audio/webm;base64,..." }
                audio_data = req.get("audio", "")
                if not audio_data:
                    self._json({"ok": False, "error": "Ses verisi eksik"}); return
                try:
                    # data URL formatını çöz
                    if "," in audio_data:
                        header, b64 = audio_data.split(",", 1)
                        # MIME tipini belirle
                        mime_part = header.split(";")[0].replace("data:", "")
                        ext_map   = {"audio/webm":"webm","audio/ogg":"ogg",
                                     "audio/wav":"wav","audio/mpeg":"mp3","audio/mp4":"m4a"}
                        ext = ext_map.get(mime_part, "webm")
                    else:
                        b64 = audio_data
                        ext = "webm"
                    import base64 as _b64
                    audio_bytes = _b64.b64decode(b64)
                    # Eski anons dosyalarını temizle (max 5 dosya tut)
                    old_files = sorted(
                        [f for f in os.listdir(ANNOUNCE_DIR) if f.startswith("anons_")],
                        key=lambda f: os.path.getmtime(os.path.join(ANNOUNCE_DIR, f))
                    )
                    while len(old_files) >= 5:
                        try: os.remove(os.path.join(ANNOUNCE_DIR, old_files.pop(0)))
                        except Exception: pass
                    fname = f"anons_{int(time.time()*1000)}.{ext}"
                    fpath = os.path.join(ANNOUNCE_DIR, fname)
                    with open(fpath, "wb") as f:
                        f.write(audio_bytes)
                    # Anons başlamadan önce her şeyi durdur (zil, tören, müzik)
                    _bell_scheduler.stop_remote()
                    # index.html açıksa JS tarafını da durdur
                    sse_broadcast("stop-all", {})
                    # Zil/toren ile ayni mantik:
                    # Her zaman SSE gonder (index.html aciksa calar)
                    # Ayrica pencere kapali ise pygame ile de dogrudan cal
                    sse_broadcast("play-announce", {
                        "url": f"/api/remote/announce-audio?file={fname}&pin={pin}"
                    })
                    if not _is_window_open():
                        print(f"[Anons] index.html kapali - pygame ile calinacak: {fname}")
                        _bell_scheduler.play_announce_file(fpath)
                    else:
                        print(f"[Anons] index.html acik - SSE ile gonderildi: {fname}")
                    self._json({"ok": True, "file": fname})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)

            elif p == "/api/remote/stream-start":
                # Canlı anons başlamadan önce zil/müzik/tören durdur
                _bell_scheduler.stop_remote()
                sse_broadcast("stop-all", {})
                mime = req.get("mime", "audio/webm")
                _live_announce.start(mime)
                self._json({"ok": True})

            elif p == "/api/remote/stream-chunk":
                chunk_b64 = req.get("chunk", "")
                if chunk_b64:
                    try:
                        import base64 as _b64
                        _live_announce.push(_b64.b64decode(chunk_b64))
                    except Exception:
                        pass
                self._json({"ok": True})

            elif p == "/api/remote/stream-end":
                _live_announce.stop()
                self._json({"ok": True})

            elif p == "/api/remote/webrtc-offer":
                sdp  = req.get("sdp", "")
                kind = req.get("type", "offer")
                if not sdp:
                    self._json({"ok": False, "error": "SDP eksik"}); return
                import asyncio as _aio
                loop = _ensure_webrtc_loop()

                async def _do_offer(_sdp=sdp, _kind=kind):
                    from aiortc import RTCPeerConnection, RTCSessionDescription
                    _wlog("_do_offer başladı")
                    if _webrtc_state["pc"] is not None:
                        try: await _webrtc_state["pc"].close()
                        except: pass
                    if _webrtc_state["receiver"] is not None:
                        _webrtc_state["receiver"].stop()
                    receiver = _WebRTCReceiver()
                    pc = RTCPeerConnection()

                    @pc.on("track")
                    def on_track(track):
                        _wlog(f"on_track tetiklendi — kind={track.kind}")
                        if track.kind == "audio":
                            loop.create_task(receiver.receive(track))

                    @pc.on("connectionstatechange")
                    async def on_state():
                        _wlog(f"connectionstatechange: {pc.connectionState}")
                        if pc.connectionState in ("failed", "closed", "disconnected"):
                            receiver.stop()

                    @pc.on("iceconnectionstatechange")
                    async def on_ice():
                        _wlog(f"iceconnectionstatechange: {pc.iceConnectionState}")

                    await pc.setRemoteDescription(RTCSessionDescription(sdp=_sdp, type=_kind))
                    _wlog(f"setRemoteDescription tamam — iceGathering={pc.iceGatheringState}")
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    _wlog("setLocalDescription (answer) tamam — ICE bekleniyor")
                    for _ in range(50):   # ICE tamamlanana kadar bekle (max 5sn)
                        if pc.iceGatheringState == "complete": break
                        await _aio.sleep(0.1)
                    _wlog(f"ICE gathering bitti — state={pc.iceGatheringState}, conn={pc.connectionState}")
                    _webrtc_state["pc"]       = pc
                    _webrtc_state["receiver"] = receiver
                    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

                try:
                    _bell_scheduler.stop_remote()
                    sse_broadcast("stop-all", {})
                    future = _aio.run_coroutine_threadsafe(_do_offer(), loop)
                    result = future.result(timeout=12)
                    self._json({"ok": True, **result})
                except Exception as _exc:
                    import traceback; traceback.print_exc()
                    self._json({"ok": False, "error": str(_exc)}, 500)

            elif p == "/api/remote/webrtc-debug":
                pc = _webrtc_state.get("pc")
                rv = _webrtc_state.get("receiver")
                self._json({
                    "ok": True,
                    "pc_conn":    pc.connectionState    if pc else "none",
                    "pc_ice":     pc.iceConnectionState if pc else "none",
                    "pc_gather":  pc.iceGatheringState  if pc else "none",
                    "receiver":   "active" if rv else "none",
                    "log":        _webrtc_log[-30:]
                })

            elif p == "/api/remote/webrtc-stop":
                import asyncio as _aio
                loop = _ensure_webrtc_loop()
                if _webrtc_state["pc"] is not None:
                    _aio.run_coroutine_threadsafe(_webrtc_state["pc"].close(), loop)
                    _webrtc_state["pc"] = None
                if _webrtc_state["receiver"] is not None:
                    _webrtc_state["receiver"].stop()
                    _webrtc_state["receiver"] = None
                self._json({"ok": True})

            elif p == "/api/remote/set-pin":
                new_pin = str(req.get("newPin", "")).strip()
                if len(new_pin) < 4 or len(new_pin) > 12 or not new_pin.isdigit():
                    self._json({"ok": False, "error": "PIN 4-12 haneli rakam olmalı"}); return
                save_remote_pin(new_pin)
                self._json({"ok": True})

            elif p == "/api/remote/fix-firefox":
                # Diğer bilgisayardaki Firefox için sertifika kurulum .bat dosyası döndür.
                # Bat dosyası: sertifikayı HTTP ile indirir, Firefox certutil ile profile ekler.
                ip   = get_local_ip()
                http_port = PORT  # 8765
                bat = (
                    "@echo off\r\n"
                    "chcp 65001 >nul\r\n"
                    "title Firefox Sertifika Kurulumu\r\n"
                    "echo.\r\n"
                    "echo  Okul Zil Sistemi - Firefox Sertifika Kurulumu\r\n"
                    "echo  ================================================\r\n"
                    "echo.\r\n"
                    "\r\n"
                    ":: Sertifikayı indir\r\n"
                    f"set CERT_URL=http://{ip}:{http_port}/api/remote/install-cert\r\n"
                    "set CERT_FILE=%TEMP%\\zil_cert.crt\r\n"
                    "echo [1/3] Sertifika indiriliyor...\r\n"
                    "powershell -Command \"Invoke-WebRequest -Uri '%CERT_URL%' -OutFile '%CERT_FILE%'\" >nul 2>&1\r\n"
                    "if not exist \"%CERT_FILE%\" (\r\n"
                    "  echo HATA: Sertifika indirilemedi. Sunucunun acik oldugunu kontrol edin.\r\n"
                    "  pause & exit /b 1\r\n"
                    ")\r\n"
                    "echo [1/3] Sertifika indirildi.\r\n"
                    "\r\n"
                    ":: Firefox certutil.exe bul\r\n"
                    "set FF_CERTUTIL=\r\n"
                    "for %%P in (\r\n"
                    "  \"C:\\Program Files\\Mozilla Firefox\\certutil.exe\"\r\n"
                    "  \"C:\\Program Files (x86)\\Mozilla Firefox\\certutil.exe\"\r\n"
                    ") do if exist %%P set FF_CERTUTIL=%%~P\r\n"
                    "\r\n"
                    "if not defined FF_CERTUTIL (\r\n"
                    "  echo HATA: Firefox certutil bulunamadi. Firefox kurulu mu?\r\n"
                    "  pause & exit /b 1\r\n"
                    ")\r\n"
                    "echo [2/3] Firefox certutil bulundu.\r\n"
                    "\r\n"
                    ":: Firefox profillerine sertifika ekle\r\n"
                    "echo [3/3] Firefox profillerine sertifika ekleniyor...\r\n"
                    "set FF_PROFILES=%APPDATA%\\Mozilla\\Firefox\\Profiles\r\n"
                    "set ADDED=0\r\n"
                    "for /d %%D in (\"%FF_PROFILES%\\*\") do (\r\n"
                    "  \"%FF_CERTUTIL%\" -A -n \"OkulZilSistemi\" -t \"CT,,\" -i \"%CERT_FILE%\" -d \"sql:%%D\" >nul 2>&1\r\n"
                    "  if not errorlevel 1 (\r\n"
                    "    echo    + Eklendi: %%~nxD\r\n"
                    "    set ADDED=1\r\n"
                    "  )\r\n"
                    ")\r\n"
                    "\r\n"
                    "if \"%ADDED%\"==\"0\" (\r\n"
                    "  echo UYARI: Hicbir profile eklenemedi.\r\n"
                    ") else (\r\n"
                    "  echo.\r\n"
                    "  echo  TAMAMLANDI! Firefox'u kapatip yeniden acin.\r\n"
                    "  echo  Artik https://{ip}:8766/remote.html adresine\r\n"
                    "  echo  uyari almadan baglanabilirsiniz.\r\n"
                    ")\r\n"
                    "echo.\r\n"
                    "pause\r\n"
                )
                data = bat.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=firefox_sertifika_kur.bat")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

            elif p == "/api/remote/toggle-holiday":
                import datetime as _dt4
                _now4   = _dt4.datetime.now()
                _today4 = f"{_now4.year}-{_now4.month:02d}-{_now4.day:02d}"
                try:
                    with open(SETTINGS_FILE, "r", encoding="utf-8") as _f4:
                        _s4 = json.load(_f4)
                except Exception:
                    _s4 = {}
                _hols4   = _s4.get("holidays") or []
                _was_hol = any(h.get("date") == _today4 for h in _hols4)
                if _was_hol:
                    _hols4 = [h for h in _hols4 if h.get("date") != _today4]
                    _now_hol = False
                else:
                    _hols4.append({"date": _today4, "description": "Uzaktan tatil"})
                    _now_hol = True
                _s4["holidays"] = _hols4
                with open(SETTINGS_FILE, "w", encoding="utf-8") as _f4w:
                    json.dump(_s4, _f4w, ensure_ascii=False, indent=2)
                _bell_scheduler.notify_settings_changed()
                self._json({"ok": True, "is_holiday": _now_hol, "date": _today4})

        else:
            self.send_response(404); self.end_headers()

    def _json(self, data, code=200):
        b = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-type","application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers(); self.wfile.write(b)

    def _file(self, path, default):
        try:
            b = open(path,encoding="utf-8").read().encode() if os.path.exists(path) \
                else json.dumps(default,ensure_ascii=False).encode()
        except Exception as e:
            b = json.dumps({"error":str(e)}).encode()
        self.send_response(200)
        self.send_header("Content-type","application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers(); self.wfile.write(b)

    def _save(self, path):
        n = int(self.headers["Content-Length"])
        try:
            data = json.loads(self.rfile.read(n).decode())
            if path == SETTINGS_FILE:
                # Sunucu tarafından yazılan alanları koru — JS bunları göndermez
                try:
                    with open(path, "r", encoding="utf-8") as _f:
                        _existing = json.load(_f)
                    for _key in ("customMelodyPath", "ntfyTopic"):
                        if _key in _existing and _key not in data:
                            data[_key] = _existing[_key]
                except Exception:
                    pass
            json.dump(data, open(path,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
            # Settings degistiyse BellScheduler'i aninda guncelle
            if path == SETTINGS_FILE:
                _bell_scheduler.notify_settings_changed()
            self._json({"success":True})
        except Exception as e: self._json({"success":False,"error":str(e)},500)

    def _try(self, fn):
        try: fn(); self._json({"success":True})
        except Exception as e: self._json({"success":False,"error":str(e)},500)

class ReuseAddrTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True  # Ana process kapanınca thread'ler de kapanır

def start_server():
    try:
        with ReuseAddrTCPServer(("0.0.0.0", PORT), ZilHandler) as httpd:
            httpd.serve_forever()
    except OSError as e:
        print(f"[HATA] Sunucu başlatılamadı: {e}")
        _shutdown_event.set()

def start_https_server():
    """HTTPS sunucusu — mikrofon erişimi için gerekli."""
    global _HTTPS_RUNNING
    if not _SSL_AVAILABLE:
        print("[HTTPS] SSL sertifikası oluşturulamadı, HTTPS devre dışı.")
        return
    try:
        import ssl
        with ReuseAddrTCPServer(("0.0.0.0", HTTPS_PORT), ZilHandler) as httpsd:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(SSL_CERT, SSL_KEY)
            httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
            _HTTPS_RUNNING = True   # port dinleniyor
            print(f"[HTTPS] Sunucu başladı → https://0.0.0.0:{HTTPS_PORT}")
            httpsd.serve_forever()
    except Exception as e:
        _HTTPS_RUNNING = False
        print(f"[HTTPS] Başlatılamadı: {e}")

_webview_window  = None   # geriye dönük uyumluluk
_webview_proc    = None   # subprocess ile açılan tarayıcı process

# Tarayıcı penceresi için subprocess takibi
_webview_proc_lock = threading.Lock()
_webview_launching    = False   # ayni anda sadece bir _launch_webview_proc calısın

def _find_browser():
    """Sistemde kurulu Chrome, Edge veya Firefox exe yolunu döndürür.
    Chrome/Edge önceliklidir (--app= modu daha iyi görünüm sağlar).
    Sadece Firefox varsa onu döndürür.
    """
    local = os.environ.get("LOCALAPPDATA", "")
    program_files   = os.environ.get("PROGRAMFILES",   r"C:\Program Files")
    program_files86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

    # 1. Önce Chrome / Edge dene
    chrome_edge = [
        os.path.join(program_files,   "Google",    "Chrome",  "Application", "chrome.exe"),
        os.path.join(program_files86, "Google",    "Chrome",  "Application", "chrome.exe"),
        os.path.join(program_files,   "Microsoft", "Edge",    "Application", "msedge.exe"),
        os.path.join(program_files86, "Microsoft", "Edge",    "Application", "msedge.exe"),
    ]
    if local:
        chrome_edge += [
            os.path.join(local, "Google",    "Chrome", "Application", "chrome.exe"),
            os.path.join(local, "Microsoft", "Edge",   "Application", "msedge.exe"),
        ]
    for exe in chrome_edge:
        if os.path.exists(exe):
            return exe

    # 2. Firefox — Chrome/Edge yoksa son çare
    firefox_candidates = [
        os.path.join(program_files,   "Mozilla Firefox", "firefox.exe"),
        os.path.join(program_files86, "Mozilla Firefox", "firefox.exe"),
    ]
    if local:
        firefox_candidates.append(
            os.path.join(local, "Mozilla Firefox", "firefox.exe")
        )
    for exe in firefox_candidates:
        if os.path.exists(exe):
            return exe

    return None


def _is_firefox(browser_exe: str) -> bool:
    """Verilen tarayıcı yolu Firefox mu?"""
    return browser_exe is not None and "firefox" in browser_exe.lower()

def _launch_webview_proc():
    """
    Chrome/Edge'i --app= moduyla, Firefox'u -kiosk moduyla AYRI bir subprocess olarak açar.
    Ana Python process'i etkilenmez — X'e basınca sadece
    bu subprocess kapanır, tray ve BellScheduler yaşamaya devam eder.
    """
    global _webview_proc, _webview_launching
    with _webview_proc_lock:
        if _webview_launching:
            print("[Window] Zaten baslatiliyor, ikinci istek yoksayildi.")
            return
        _webview_launching = True

    _profile_dir = os.path.join(DATA_DIR, "browser_profile")
    os.makedirs(_profile_dir, exist_ok=True)

    browser_exe = _find_browser()

    if browser_exe:
        try:
            if _is_firefox(browser_exe):
                # Firefox: --kiosk modu (pencere görünümü), profil ile ayrı oturum
                ff_profile = os.path.join(DATA_DIR, "firefox_profile")
                os.makedirs(ff_profile, exist_ok=True)
                args = [
                    browser_exe,
                    "-profile", ff_profile,
                    "-new-instance",
                    "-width",  "1280",
                    "-height", "900",
                    APP_URL,
                ]
                print("[Window] Firefox ile açılıyor.")
            else:
                # Chrome / Edge: --app= modu (çerçevesiz pencere)
                args = [
                    browser_exe,
                    "--app=" + APP_URL,
                    "--start-maximized",
                    "--disable-session-crashed-bubble",
                    "--no-first-run",
                    "--disable-features=TranslateUI",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies",
                    "--user-data-dir=" + _profile_dir,
                ]
            proc = subprocess.Popen(args)
            with _webview_proc_lock:
                _webview_proc = proc
            proc.wait()  # tarayıcı kapanınca biter, ana process etkilenmez
        except Exception as e:
            print(f"[Window] Tarayıcı başlatılamadı: {e}")
            webbrowser.open(APP_URL)
    else:
        # Hiçbir tarayıcı bulunamadı — varsayılan tarayıcıda aç
        print("[Window] Tarayıcı bulunamadı, varsayılan tarayıcı açılıyor.")
        webbrowser.open(APP_URL)

    with _webview_proc_lock:
        _webview_proc    = None
        _webview_launching = False


def _open_browser_fallback():
    """Geriye dönük uyumluluk için korundu."""
    threading.Thread(target=_launch_webview_proc, daemon=True).start()


def show_window():
    """Tray ikonuna cift tikladiginda veya menuiden cagrilir.
    Pencere aciksa one getirir, kapali / bulunamazsa yeni acar."""
    print("[Window] show_window cagrıldı")
    with _webview_proc_lock:
        proc       = _webview_proc
        launching  = _webview_launching

    if launching:
        print("[Window] Zaten baslatiliyor.")
        return

    if proc and proc.poll() is None:
        # Subprocess calisıyor — pencereyi one getirmeyi dene
        brought = False
        try:
            import ctypes, ctypes.wintypes
            u32 = ctypes.windll.user32

            def _try_hwnd(hwnd):
                if not hwnd:
                    return False
                u32.ShowWindow(hwnd, 9)   # SW_RESTORE
                u32.SetForegroundWindow(hwnd)
                print(f"[Window] Pencere one getirildi: hwnd={hwnd}")
                return True

            # 1) Tam baslik eslesmesi
            brought = _try_hwnd(u32.FindWindowW(None, "Okul Zil Sistemi"))

            # 2) EnumWindows ile baslik icinde arama
            if not brought:
                found_hwnd = [0]

                @ctypes.WINFUNCTYPE(ctypes.c_bool,
                                    ctypes.wintypes.HWND,
                                    ctypes.wintypes.LPARAM)
                def _cb(hwnd, _lp):
                    length = u32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        u32.GetWindowTextW(hwnd, buf, length + 1)
                        if "Okul Zil" in buf.value or "NeneOkul" in buf.value:
                            found_hwnd[0] = hwnd
                            return False
                    return True

                u32.EnumWindows(_cb, 0)
                if found_hwnd[0]:
                    brought = _try_hwnd(found_hwnd[0])

            # 3) Chrome / Edge sinif adi
            if not brought:
                brought = _try_hwnd(u32.FindWindowW("Chrome_WidgetWin_1", None))

            # 4) Firefox
            if not brought:
                brought = _try_hwnd(u32.FindWindowW("MozillaWindowClass", None))

        except Exception as e:
            print(f"[Window] One getirme hatasi: {e}")

        if brought:
            return
        print("[Window] Pencere bulunamadi, yeniden aciliyor.")

    # Pencere kapali veya bulunamadi — yeni ac
    print("[Window] Yeni pencere aciliyor.")
    threading.Thread(target=_launch_webview_proc, daemon=True).start()
_exit_requested = threading.Event()

def _kill_children():
    """Tüm alt processleri kapat."""
    try:
        with _webview_proc_lock:
            proc = _webview_proc
        if proc:
            proc.terminate()
    except Exception:
        pass

def _exit_app():
    """Tray menüsü -> Çıkış."""
    global _should_exit
    allow_sleep()
    remove_wake_task()
    _bell_scheduler.stop()
    _should_exit = True
    # WebView2 subprocess'ini kapat
    try:
        with _webview_proc_lock:
            proc = _webview_proc
        if proc and proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    try:
        if _tray_icon:
            _tray_icon.stop()
    except Exception:
        pass
    time.sleep(0.3)
    os._exit(0)

def open_window():
    """Ana thread'de çağrılır. WebView2'yi subprocess olarak başlatır,
    ardından _shutdown_event bekler (program çalıştığı sürece bloklar)."""
    # WebView2'yi ayrı thread'de başlat (ana thread serbest kalsın)
    threading.Thread(target=_launch_webview_proc, daemon=True).start()
    # Ana thread burada bekler — tray ve scheduler arka planda çalışır
    _shutdown_event.wait()

# ============================================================
# UYKU ENGELLEME — SetThreadExecutionState (yalnizca Windows)
# ============================================================
# Bayrak açıklamaları:
#   _ES_CONTINUOUS      : Ayarı sıfırlanana kadar kalıcı uygula
#   _ES_SYSTEM_REQUIRED : Windows'un (CPU/RAM) uyku/hibernate moduna girmesini engelle
#   _ES_DISPLAY_REQUIRED: Monitörün kapanmasını da engeller — KULLANILMIYOR,
#                         ekran güç planına göre serbestçe kapanabilir.
#                         Zil mantığı Web Worker'da çalıştığından ekran
#                         kapalıyken de zil kaçmaz.
_ES_CONTINUOUS      = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
# _ES_DISPLAY_REQUIRED = 0x00000002  — kasıtlı devre dışı
_WAKE_TASK_NAME     = "OkulZilSistemi_SabahUyan"

def prevent_sleep():
    """Program çalıştığı sürece Windows'un sistem uyku/hibernate moduna
    girmesini engeller.  Monitör güç planına göre serbestçe kapanabilir;
    zil mantığı index.html içindeki Web Worker'da çalıştığından ekran
    kapalı olsa bile checkForBells() saniyede bir çağrılmaya devam eder.
    """
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
        )
    except Exception:
        pass

def allow_sleep():
    """Program kapanırken uyku engelini tamamen kaldırır."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass

def register_wake_task():
    """Her sabah 08:00'da bilgisayari uyandiran Gorev Zamanlayici gorevi olusturur.
    Ekran kapali kalir; sadece sistem uyanir (Wake Timer).
    """
    if platform.system() != "Windows":
        return
    try:
        # Gorev XML'i — WakeToRun ile sistem uyandirilir, eylem bos (cmd /c exit)
        xml = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            '<Triggers><CalendarTrigger>'
            '<StartBoundary>2000-01-01T08:00:00</StartBoundary>'
            '<Enabled>true</Enabled>'
            '<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>'
            '</CalendarTrigger></Triggers>'
            '<Settings>'
            '<WakeToRun>true</WakeToRun>'
            '<ExecutionTimeLimit>PT1M</ExecutionTimeLimit>'
            '<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>'
            '<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
            '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>'
            '</Settings>'
            '<Actions Context="Author">'
            '<Exec><Command>cmd.exe</Command><Arguments>/c exit</Arguments></Exec>'
            '</Actions>'
            '<Principals><Principal id="Author">'
            '<LogonType>InteractiveToken</LogonType>'
            '<RunLevel>LeastPrivilege</RunLevel>'
            '</Principal></Principals>'
            '</Task>'
        )
        # Gecici XML dosyasina yaz
        xml_path = os.path.join(SCRIPT_DIR, "_wake_task.xml")
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(xml)

        # Once varsa sil, sonra olustur
        subprocess.run(
            ["schtasks", "/Delete", "/TN", _WAKE_TASK_NAME, "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        result = subprocess.run(
            ["schtasks", "/Create", "/TN", _WAKE_TASK_NAME, "/XML", xml_path, "/F"],
            capture_output=True, text=True
        )
        os.remove(xml_path)

        if result.returncode == 0:
            pass
        else:
            pass
    except Exception:
        pass

def remove_wake_task():
    """Program kapanirken uyanma gorevini siler."""
    if platform.system() != "Windows":
        return
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", _WAKE_TASK_NAME, "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2
        )
    except Exception:
        pass

# SYSTEM TRAY  — pystray
# ============================================================
_tray_icon = None

def _make_image():
    """zill.ico dosyasından tray ikonu yükler; bulunamazsa çizilmiş yedek kullanır."""
    from PIL import Image
    ico_path = os.path.join(SCRIPT_DIR, "zill.ico")
    try:
        img = Image.open(ico_path)
        # ICO dosyası birden fazla boyut içerebilir; 64x64 en uygununu seç
        img = img.convert("RGBA")
        img = img.resize((64, 64), Image.LANCZOS)
        return img
    except Exception:
        # zill.ico bulunamazsa veya açılamazsa elle çiz
        from PIL import ImageDraw
        size = 64
        img  = Image.new("RGBA", (size, size), (0,0,0,0))
        d    = ImageDraw.Draw(img)
        d.ellipse([2,2,62,62], fill="#f59e0b", outline="#92400e", width=2)
        d.ellipse([18,12,46,40], fill="white")
        d.rectangle([14,36,50,46], fill="white")
        d.rectangle([29,4,35,14], fill="white")
        d.ellipse([25,44,39,56], fill="white")
        return img

def run_tray():
    global _tray_icon
    import pystray

    # Debounce: ayni anda birden fazla tetiklenmeyi engelle
    _last_open = [0.0]

    def _open_once(icon=None, item=None):
        now = time.time()
        if now - _last_open[0] < 1.0:   # 1 saniye icinde tekrar tetiklenirse yoksay
            print("[Tray] Debounce — yoksayildi")
            return
        _last_open[0] = now
        print("[Tray] Pencere aciliyor (default menuItem / cift tiklama)")
        show_window()

    def _build_menu():
        return pystray.Menu(
            pystray.MenuItem(
                "Okul Zil Sistemini Ac",
                _open_once,
                default=True,   # cift tiklama ve tek tiklama ikisi de buraya gelir
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Cikis", lambda icon, item: _exit_app()),
        )

    _tray_icon = pystray.Icon(
        APP_NAME,
        _make_image(),
        "Okul Zil Sistemi",
        menu=_build_menu(),
    )
    _tray_icon.run()

# ============================================================
# NTFY.SH — Mevcut IP'yi buluta yayınla (mobil bağlantı köprüsü)
# ============================================================

def _get_or_create_ntfy_topic() -> str:
    """settings.json'dan ntfyTopic okur; yoksa benzersiz bir ID üretip kaydeder."""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            if s.get("ntfyTopic"):
                return s["ntfyTopic"]
    except Exception:
        pass
    # Benzersiz ID üret: "nene-okul-zil-" + 8 rasgele karakter
    new_topic = "nene-okul-zil-" + secrets.token_hex(4)
    try:
        s = {}
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
        s["ntfyTopic"] = new_topic
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        print(f"[ntfy] Yeni topic olusturuldu: {new_topic}")
    except Exception as e:
        print(f"[ntfy] Topic kaydedilemedi: {e}")
    return new_topic

_NTFY_TOPIC = None  # İlk çalışmada _get_or_create_ntfy_topic() ile doldurulur

def publish_ip_to_ntfy():
    """Program açılınca mevcut IP'yi ntfy.sh'e gönderir.
    Telefon kısayolu (GitHub Pages) buradan okuyarak PC'ye yönlenir."""
    global _NTFY_TOPIC
    import urllib.request
    _NTFY_TOPIC = _get_or_create_ntfy_topic()
    for attempt in range(4):
        try:
            ip = get_local_ip()
            url = "https://ntfy.sh/" + _NTFY_TOPIC
            req = urllib.request.Request(url, data=ip.encode("utf-8"), method="POST")
            req.add_header("Title", "OkulZil-IP")
            req.add_header("Content-Type", "text/plain")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    print(f"[ntfy] IP yayinlandi: {ip}")
                    return
        except Exception as e:
            print(f"[ntfy] Deneme {attempt + 1} basarisiz: {e}")
            time.sleep(3)
    print("[ntfy] IP yayinlanamadi (internet baglantisi kontrol edin)")

# ============================================================
# RENDER KÖPRÜ — WebSocket bağlantısı
# ============================================================
BRIDGE_URL = "wss://okulzil-bridge.onrender.com"

def _bridge_worker():
    """Render köprüsüne WebSocket bağlantısı açar.
    Gelen HTTP proxy isteklerini yerel HTTP sunucusuna iletir."""
    import urllib.request, urllib.error
    try:
        import websocket  # websocket-client
    except ImportError:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install",
                          "websocket-client", "--quiet"],
                         capture_output=True, timeout=60)
            import websocket
        except Exception as e:
            print(f"[Köprü] websocket-client kurulamadı: {e}")
            return

    topic = _get_or_create_ntfy_topic()
    url   = f"{BRIDGE_URL}/ws/school/{topic}"
    print(f"[Köprü] Bağlanıyor: {url}")

    retry_delay = 5

    while not _shutdown_event.is_set():
        try:
            ws = websocket.create_connection(url, timeout=10)
            print(f"[Köprü] Bağlandı — topic: {topic}")
            retry_delay = 5

            while not _shutdown_event.is_set():
                try:
                    ws.settimeout(20)
                    raw = ws.recv()
                    if not raw:
                        break
                    try:
                        packet = json.loads(raw)
                    except Exception:
                        continue

                    req_id   = packet.get("_req_id")
                    method   = packet.get("_method", "GET")
                    endpoint = packet.get("_endpoint", "/")
                    qs       = packet.get("_qs", "")

                    # SSE başlat/durdur sinyalleri (şimdilik yoksay — SSE yerel)
                    if packet.get("_sse_start") or packet.get("_sse_stop"):
                        continue

                    # Paketi local HTTP sunucusuna ilet
                    local_url = f"http://127.0.0.1:{PORT}{endpoint}"
                    if qs:
                        local_url += "?" + qs

                    # PIN'i header'a ekle
                    pin = packet.get("pin", packet.get("X-Remote-Pin", ""))
                    body_data = {k: v for k, v in packet.items()
                                 if not k.startswith("_") and k not in ("pin",)}
                    body_bytes = json.dumps({**body_data, "pin": pin},
                                           ensure_ascii=False).encode()

                    try:
                        req = urllib.request.Request(
                            local_url,
                            data=body_bytes if method == "POST" else None,
                            method=method
                        )
                        req.add_header("Content-Type", "application/json")
                        req.add_header("X-Remote-Pin", pin)
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            resp_body = resp.read().decode("utf-8", errors="replace")
                            status    = resp.status
                    except urllib.error.HTTPError as he:
                        resp_body = he.read().decode("utf-8", errors="replace")
                        status    = he.code
                    except Exception as e:
                        resp_body = json.dumps({"ok": False, "error": str(e)})
                        status    = 500

                    # Cevabı köprüye gönder
                    if req_id:
                        try:
                            reply = json.loads(resp_body)
                        except Exception:
                            reply = {"raw": resp_body}
                        reply["_req_id"]  = req_id
                        reply["_status"]  = status
                        ws.send(json.dumps(reply, ensure_ascii=False))

                except websocket.WebSocketTimeoutException:
                    # Keepalive ping
                    try:
                        ws.ping()
                    except Exception:
                        break
                except Exception as e:
                    print(f"[Köprü] Bağlantı hatası: {e}")
                    break

            try:
                ws.close()
            except Exception:
                pass
            print("[Köprü] Bağlantı kesildi, yeniden deneniyor…")

        except Exception as e:
            print(f"[Köprü] Bağlanamadı: {e}")

        if not _shutdown_event.is_set():
            _shutdown_event.wait(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

    print("[Köprü] Durduruldu.")



# ============================================================
# ANA PROGRAM
# ============================================================
if __name__ == "__main__":

    # ── Tek Örnek Kontrolü ───────────────────────────────────────────────────
    if not _acquire_single_instance_mutex():
        import ctypes as _ctypes
        _ctypes.windll.user32.MessageBoxW(
            0,
            "Okul Zil Sistemi zaten çalışıyor!\n\n"
            "Sistem tepsisinde (saat yanında) simgesine tıklayarak\n"
            "mevcut pencereyi açabilirsiniz.",
            "Zaten Çalışıyor",
            0x30 | 0x1000   # MB_ICONWARNING | MB_SYSTEMMODAL
        )
        sys.exit(0)
    # ─────────────────────────────────────────────────────────────────────────

    prevent_sleep()  # Uyku modunu engelle

    # Kayıtlı ayarlardan morningWakeEnabled'ı oku
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            _saved = json.load(f)
        if _saved.get("morningWakeEnabled", False):
            register_wake_task()
        else:
            remove_wake_task()  # Önceden kalmış görevi temizle
    except Exception:
        remove_wake_task()  # Dosya yoksa veya hata varsa görevi temizle

    threading.Thread(target=start_server, daemon=True).start()
    threading.Thread(target=publish_ip_to_ntfy, daemon=True).start()
    threading.Thread(target=_bridge_worker, daemon=True).start()

    # Python tarafli zil zamanlayicisini baslat (WebView2'dan bagimsiz)
    _bell_scheduler.start()

    # SSL sertifikası oluştur, güvenilir depoya ekle ve HTTPS sunucusunu başlat
    _SSL_AVAILABLE = _ensure_ssl_cert()
    if _SSL_AVAILABLE:
        _install_cert_trusted()   # Sertifikayı Windows'a otomatik tanıt (bir kez UAC sorar)
        threading.Thread(target=start_https_server, daemon=True).start()

    # Pystray'i thread'de calistir (webview ana thread'e ihtiyac duyar)
    if platform.system() == "Windows":
        try:
            import pystray as _pt  # noqa
            threading.Thread(target=run_tray, daemon=True).start()
        except ImportError:
            pass

    # open_window() WebView2'yi subprocess'te başlatır, ardından _shutdown_event bekler
    try:
        open_window()
    except KeyboardInterrupt:
        allow_sleep()
        remove_wake_task()
        _bell_scheduler.stop()
        os._exit(0)
