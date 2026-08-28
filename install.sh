#!/usr/bin/env bash
# =============================================================================
#  Skill Matcher (skill-matcher) one-shot installer
#
#  One project, two editions:
#    - SKILL.md edition  -> WorkBuddy / Claude Code / CodeBuddy / ~/.skills
#    - dsh plugin edition-> DeepSeek Harness (~/.dsh/profiles/*)
#
#  Usage:
#    bash install.sh                          # local run, auto-detect & install
#    bash install.sh --skill-only             # SKILL.md edition only
#    bash install.sh --dsh-only               # dsh plugin edition only
#    bash install.sh --dry-run                # preview only, change nothing
#    bash install.sh --help
#
#  Remote one-liner (no manual download):
#    curl -fsSL https://raw.githubusercontent.com/axel286137079-dot/skill-matcher/main/install.sh | bash
#
#  Notes:
#    - Installing copies files only; existing skills/configs are untouched
#      (dsh profile package.json is backed up before any change)
#    - dsh edition needs a dsh web restart to take effect (printed at the end)
#    - Uninstall: delete the installed directories; for dsh also remove the
#      dependency line + node_modules dir
# =============================================================================
set -euo pipefail

VERSION="1.0.0"
PROJECT_NAME="skill-matcher"
DISPLAY_NAME="Skill Matcher"

# ---------- args ----------
MODE_SKILL=1
MODE_DSH=1
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --skill-only) MODE_DSH=0 ;;
    --dsh-only)   MODE_SKILL=0 ;;
    --dry-run)    DRY_RUN=1 ;;
    --help|-h)
      sed -n '3,19p' "$0"
      exit 0 ;;
    *) echo "unknown argument: $arg (see --help)" >&2; exit 1 ;;
  esac
done

say()  { printf '\033[1;34m[skill-matcher]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
skip() { printf '\033[1;90m[-]\033[0m %s\n' "$*"; }

# ---------- locate project root ----------
find_src() {
  local here
  if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
    if [[ -f "$here/SKILL.md" ]]; then SRC="$here"; return; fi
  fi
  # remote mode (curl | bash)
  if ! command -v git >/dev/null 2>&1; then
    warn "git is required for remote install; download the project zip and run locally instead"
    exit 1
  fi
  local tmp="$HOME/.skill-matcher-src"
  if [[ ! -f "$tmp/SKILL.md" ]]; then
    say "remote mode: fetching project source (first time) ..."
    git clone --depth 1 https://github.com/axel286137079-dot/skill-matcher.git "$tmp" 2>/dev/null \
      || { warn "clone failed; check network or install locally"; exit 1; }
  else
    say "remote mode: using cached source at $tmp (delete it to refresh)"
  fi
  SRC="$tmp"
}

find_src
say "project root: $SRC  ($PROJECT_NAME v$VERSION / $DISPLAY_NAME)"
[[ $DRY_RUN -eq 1 ]] && say "=== DRY-RUN: preview only ==="

# ---------- environment detection ----------
detect_skill_dirs() {
  for d in "$HOME/.workbuddy/skills" "$HOME/.claude/skills" "$HOME/.codebuddy/skills" "$HOME/.skills"; do
    [[ -d "$d" ]] && printf '%s\n' "$d"
  done
}

detect_dsh_profiles() {
  for p in "$HOME/.dsh/profiles"/*/package.json; do
    [[ -f "$p" ]] && grep -q '"dsh"' "$p" && printf '%s\n' "$p"
  done
}

# ---------- SKILL.md edition ----------
install_skill() {
  local dirs=()
  while IFS= read -r d; do dirs+=("$d"); done < <(detect_skill_dirs)
  if [[ ${#dirs[@]} -eq 0 ]]; then
    warn "no WorkBuddy / Claude Code / CodeBuddy skills dir found (create one, or mkdir -p ~/.skills)"
    warn "SKILL.md edition skipped"
    return
  fi
  for target in "${dirs[@]}"; do
    local dst="$target/$PROJECT_NAME"
    if [[ "$(cd "$dst" 2>/dev/null && pwd)" == "$SRC" ]]; then
      skip "source dir == target ($dst); self-install skipped"
      continue
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
      say "will install SKILL.md edition -> $dst"
      continue
    fi
    mkdir -p "$dst"
    rsync -a --delete \
      --exclude='plugin/' \
      --exclude='.git/' \
      --exclude='bin/__pycache__/' \
      --exclude='index/skills.json' \
      --exclude='index/experts.json' \
      --exclude='index/contributions/' \
      --exclude='index/feedback.jsonl' \
      "$SRC/" "$dst/"
    ok "SKILL.md edition installed -> $dst"
  done
  say "tip: run 'python3 bin/sync_index.py' inside the skill dir to build the index (auto on first use)"
}

# ---------- dsh plugin edition ----------
install_dsh() {
  local profiles=()
  while IFS= read -r p; do profiles+=("$p"); done < <(detect_dsh_profiles)
  if [[ ${#profiles[@]} -eq 0 ]]; then
    warn "no DeepSeek Harness profile found (~/.dsh/profiles); dsh plugin edition skipped"
    return
  fi
  for pkg in "${profiles[@]}"; do
    local profile_dir="$(dirname "$pkg")"
    local dep_path="$SRC/plugin"
    if [[ $DRY_RUN -eq 1 ]]; then
      say "will install dsh plugin edition -> profile $profile_dir"
      continue
    fi
    [[ -f "$pkg" ]] && cp "$pkg" "$pkg.bak-$(date +%Y%m%d-%H%M%S)" || true
    if command -v python3 >/dev/null 2>&1; then
      python3 - "$pkg" "$dep_path" <<'PY'
import json, sys
p, dep = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(p, encoding="utf-8"))
except Exception:
    data = {}
deps = data.setdefault("dependencies", {})
deps["dsh-skill-matcher"] = "file:" + dep
bundles = data.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
if "dsh-skill-matcher" not in bundles:
    bundles.append("dsh-skill-matcher")
with open(p, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
    else
      warn "python3 not found; add dsh-skill-matcher to $pkg manually (dependencies + dsh.profile.bundles)"
      continue
    fi
    local nm="$profile_dir/node_modules/$PROJECT_NAME"
    mkdir -p "$profile_dir/node_modules"
    if [[ -d "$nm" ]]; then
      cp -R "$nm" "$nm.bak-$(date +%Y%m%d-%H%M%S)"
      rm -rf "$nm"
    fi
    cp -R "$SRC/plugin" "$nm"
    ok "dsh plugin edition installed -> $profile_dir"
  done
  warn "dsh edition: restart dsh web to activate:  launchctl kickstart -k gui/\$(id -u)/com.deepseek.harness.web"
}

# ---------- run ----------
say "installing ..."
[[ $MODE_SKILL -eq 1 ]] && install_skill
[[ $MODE_DSH -eq 1 ]] && install_dsh
say "done! $PROJECT_NAME v$VERSION ($DISPLAY_NAME)"
say "see README.md for usage / contribution / FAQ"
