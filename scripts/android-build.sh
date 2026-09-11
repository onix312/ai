#!/usr/bin/env bash
# Собрать APK оболочки кассы и положить его туда, откуда телефон скачает сам.
#
# Почему скрипт, а не «откройте Android Studio»: сборка должна повторяться
# после изменений оболочки одной командой, без ручных кликов по меню. Скрипт
# НИЧЕГО не устанавливает молча: если JDK или SDK нет, он пишет, что именно
# нужно поставить, и спрашивает разрешение (установка ~2-3 ГБ — решение
# владельца, а не наше).
#
#   ./scripts/android-build.sh              # debug-сборка → site/app/*.apk
#   ./scripts/android-build.sh --release    # релизная (нужен android/keystore.properties)
#   ./scripts/android-build.sh --install     # собрать и поставить на подключённый телефон
#   ./scripts/android-build.sh --quiet-check # только проверить окружение, ничего не собирать
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/android"
OUT="$ROOT/site/app"
MODE="debug"
DO_INSTALL=0
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --release) MODE="release" ;;
    --install) DO_INSTALL=1 ;;
    --quiet-check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Не знаю аргумент: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "✗ $*" >&2; exit 1; }
say() { echo "  $*"; }

need_gradle() {
  if [[ -x "$APP/gradlew" ]]; then echo "$APP/gradlew"; return; fi
  command -v gradle || echo ""
}

check_env() {
  local ok=1
  if ! command -v java >/dev/null 2>&1; then
    echo "✗ Не найден JDK 17 (java: command not found)."
    echo "  Ubuntu/Debian: sudo apt install openjdk-17-jdk"
    echo "  Windows/macOS: поставьте Android Studio — JDK прилагается."
    ok=0
  else
    local major
    major="$(java -version 2>&1 | head -1 | sed -E 's/.*version "([0-9]+).*/\1/')"
    if [[ -z "$major" || "$major" -lt 17 ]]; then
      echo "✗ Нужен JDK 17+, найден: ${major:-не разобрался} (AGP 8 его требует)."
      ok=0
    fi
  fi

  local sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}}"
  if [[ ! -d "$sdk" ]]; then
    echo "✗ Не найден Android SDK (ANDROID_HOME=$sdk не существует)."
    echo "  Проще всего: установить Android Studio (в нём SDK уже есть) или"
    echo "  command-line tools: https://developer.android.com/studio#command-line-tools-only"
    echo "  затем: sdkmanager \"platforms;android-34\" \"build-tools;34.0.0\" \"platform-tools\""
    ok=0
  else
    say "SDK: $sdk"
    for part in "platforms;android-34" "build-tools;34.0.0"; do
      local dir
      case "$part" in
        platforms*) dir="$sdk/platforms/android-34" ;;
        *) dir="$sdk/build-tools/34.0.0" ;;
      esac
      if [[ ! -d "$dir" ]]; then
        echo "! нет компонента: $part"
        echo "  sdkmanager \"$part\"  (и согласие на лицензии: yes | sdkmanager --licenses)"
        ok=0
      fi
    done
  fi

  if [[ -z "$(need_gradle)" ]]; then
    echo "✗ Нет ни gradle в PATH, ни обёртки android/gradlew."
    echo "  Один раз сгенерируйте обёртку: (cd android && gradle wrapper --gradle-version 8.7)"
    echo "  (jar обёртки в репозиторий не кладём — он бинарный и меняется вместе с Gradle)"
    ok=0
  fi
  if [[ "$MODE" == "release" && ! -f "$APP/keystore.properties" ]]; then
    echo "✗ --release требует android/keystore.properties (ключ подписи, см. docs/ANDROID.md)."
    ok=0
  fi
  return "$ok"
}

echo "— Проверка окружения"
if ! check_env; then
  echo
  echo "Окружение не готово. Сборка — на ПК владельца; мы ничего не ставим без вашего"
  echo "явного решения. Список того, что нужно, — выше; подробности и цены: docs/ANDROID.md"
  exit 1
fi
if [[ "$CHECK_ONLY" == "1" ]]; then echo "✓ Окружение готово (проверка, сборка не запускалась)"; exit 0; fi

GRADLE="$(need_gradle)"
VERSION_NAME="$(sed -nE "s/^[[:space:]]*versionName '([^']+)'.*/\1/p" "$APP/app/build.gradle" | head -1)"
VERSION_CODE="$(sed -nE "s/^[[:space:]]*versionCode ([0-9]+).*/\1/p" "$APP/app/build.gradle" | head -1)"
[[ -n "$VERSION_NAME" ]] || die "не удалось прочитать versionName в android/app/build.gradle"

echo "— Сборка ($MODE) · NOZZA касса $VERSION_NAME ($VERSION_CODE)"
MODE_CAP="$(tr '[:lower:]' '[:upper:]' <<<"${MODE:0:1}")${MODE:1}"
( cd "$APP" && "$GRADLE" --console=plain ":app:assemble${MODE_CAP}" )

APK="$APP/app/build/outputs/apk/$MODE/app-$MODE.apk"
if [[ ! -f "$APK" ]]; then
  # неподписанная release-сборка называется иначе: это не успех, а повод
  # завести ключ — телефон такой APK не примет
  ALT="$APP/app/build/outputs/apk/$MODE/app-$MODE-unsigned.apk"
  if [[ -f "$ALT" ]]; then
    die "APK собран без подписи ($ALT). Для релиза нужен android/keystore.properties — см. docs/ANDROID.md"
  fi
  die "сборка завершилась, но $APK не найден"
fi

umask 022   # телефон читает файл по HTTP — права на каталог данных не должны мешать
mkdir -p "$OUT"
NAME="NOZZA-kassa-${VERSION_NAME}.apk"
cp "$APK" "$OUT/$NAME"
# Манифест сборки. Кроме версии и имени файла пишем размер и sha256: по ним
# панель и оболочка проверяют, что телефон скачал именно эту сборку, а не
# обрезанный файл. Changelog берётся из CHANGELOG.md — раздел текущей версии,
# чтобы кассир видел «что нового» до установки (17.0.13).
python3 - "$OUT/version.json" "$VERSION_NAME" "$VERSION_CODE" "$NAME" \
         "$OUT/$NAME" "$ROOT/CHANGELOG.md" <<'PY'
import hashlib, json, sys, time

target, version, code, name, apk, changelog_path = sys.argv[1:7]


def section(path: str, tag: str) -> str:
    """Раздел changelog для этой версии — без Markdown-заголовка."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    marker = f"## {tag}"
    start = text.find(marker)
    if start < 0:
        return ""
    body = text[start + len(marker):]
    end = body.find("\n## ")
    if end >= 0:
        body = body[:end]
    lines = [line.strip() for line in body.strip().splitlines() if line.strip()]
    return "\n".join(lines)[:1200]


digest = hashlib.sha256()
with open(apk, "rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        digest.update(chunk)

json.dump({
    "version": version,
    "version_code": int(code),
    "file": name,
    "package": "ai.printflow.kassa",
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "size_bytes": __import__("os").path.getsize(apk),
    "sha256": digest.hexdigest(),
    "changelog": section(changelog_path, version),
}, open(target, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

SIZE="$(du -h "$OUT/$NAME" | cut -f1)"
echo
echo "✓ Готово: site/app/$NAME ($SIZE)"
echo "  На телефоне откройте кассу — появится плашка «Скачать», или напрямую:"
IP="$(python3 - <<'PY' 2>/dev/null || true
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
except Exception:
    pass
finally:
    s.close()
PY
)"
[[ -n "$IP" ]] && say "http://$IP:8765/app/$NAME" || say "http://<адрес-ПК>:8765/app/$NAME"
say "Android предложит «установить из неизвестных источников» — это нормально для LAN."

if [[ "$DO_INSTALL" == "1" ]]; then
  command -v adb >/dev/null 2>&1 || die "нет adb (platform-tools): sdkmanager \"platform-tools\""
  echo "— adb install -r"
  adb install -r "$OUT/$NAME"
  echo "✓ Поставлено на подключённый телефон"
fi
