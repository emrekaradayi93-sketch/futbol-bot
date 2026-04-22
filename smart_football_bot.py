"""
⚽ Akıllı Maç Filtresi — Telegram Botu
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sistem Mantığı:
  1. Maç öncesi favori oranı < 1.60 olan maçları takibe al
  2. İlk yarı bitti → favori gerideyse:
       • 1 gol fark → favorinin maçı kazanma oranı < 2.30  → 🟡 SINYAL
       • 2 gol fark → underdog'un maçı kazanma oranı > 1.90 → 🔴 SINYAL

API'ler:
  - api-football.com  → canlı skor + yarı bilgisi
  - the-odds-api.com  → canlı oranlar

Gereksinimler:
  pip install python-telegram-bot==20.7 aiohttp python-dotenv

.env:
  TELEGRAM_TOKEN=...
  FOOTBALL_API_KEY=...
  ODDS_API_KEY=...
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
ODDS_API_KEY     = os.getenv("ODDS_API_KEY")

FOOTBALL_BASE = "https://v3.football.api-sports.io"
ODDS_BASE     = "https://api.the-odds-api.com/v4"

# Sistem eşik değerleri
PRE_MATCH_FAV_THRESHOLD     = 1.70  # Maç öncesi max favori oranı
HT_1GOAL_WIN_THRESHOLD      = 2.40  # 1 fark → max favori FT kazanma oranı
HT_2GOAL_UNDERDOG_THRESHOLD = 1.80  # 2 fark → min underdog FT kazanma oranı

# 50–55. dakika penceresi
MINUTE_WINDOW_START = 50
MINUTE_WINDOW_END   = 55

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─── Veri Modelleri ────────────────────────────────────────────────────────────

@dataclass
class MatchOdds:
    home_win: float = 0.0
    draw: float    = 0.0
    away_win: float = 0.0

@dataclass
class TrackedMatch:
    fixture_id: int
    home_team: str
    away_team: str
    league: str
    country: str
    # Favori = pre-match oranı < 1.60 olan taraf
    favorite: str          # "home" | "away"
    pre_match_fav_odds: float
    # Durum takibi
    signal_sent: bool = False
    ht_score_home: int = 0
    ht_score_away: int = 0

# ─── Global Durum ──────────────────────────────────────────────────────────────

# fixture_id → TrackedMatch
tracked: dict[int, TrackedMatch] = {}


# ─── API Yardımcıları ──────────────────────────────────────────────────────────

async def get_todays_fixtures() -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{FOOTBALL_BASE}/fixtures?date={today}", headers=headers) as r:
            data = await r.json()
            return data.get("response", [])


async def get_live_fixtures() -> list:
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{FOOTBALL_BASE}/fixtures?live=all", headers=headers) as r:
            data = await r.json()
            return data.get("response", [])


async def get_prematch_odds(fixture_id: int) -> Optional[MatchOdds]:
    """api-football üzerinden maç öncesi 1X2 oranlarını çeker."""
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    url = f"{FOOTBALL_BASE}/odds?fixture={fixture_id}&bet=1"  # bet id 1 = Match Winner
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            data = await r.json()
            resp = data.get("response", [])
            if not resp:
                return None
            try:
                values = resp[0]["bookmakers"][0]["bets"][0]["values"]
                odds = MatchOdds()
                for v in values:
                    if v["value"] == "Home":
                        odds.home_win = float(v["odd"])
                    elif v["value"] == "Draw":
                        odds.draw = float(v["odd"])
                    elif v["value"] == "Away":
                        odds.away_win = float(v["odd"])
                return odds
            except (IndexError, KeyError, ValueError):
                return None


async def get_live_odds_odds_api(home: str, away: str) -> Optional[MatchOdds]:
    """
    The Odds API üzerinden canlı 1X2 oranlarını çeker.
    Takım adına göre eşleştirme yapar.
    """
    url = f"{ODDS_BASE}/sports/soccer/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params) as r:
            if r.status != 200:
                return None
            data = await r.json()

    home_l = home.lower()
    away_l = away.lower()

    for event in data:
        eh = event.get("home_team", "").lower()
        ea = event.get("away_team", "").lower()
        if home_l in eh or eh in home_l or away_l in ea or ea in away_l:
            for bm in event.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt["key"] == "h2h":
                        odds = MatchOdds()
                        for o in mkt["outcomes"]:
                            n = o["name"].lower()
                            if "draw" in n:
                                odds.draw = float(o["price"])
                            elif home_l in n or n in home_l:
                                odds.home_win = float(o["price"])
                            else:
                                odds.away_win = float(o["price"])
                        if odds.home_win and odds.away_win:
                            return odds
    return None


# ─── Sistem Mantığı ────────────────────────────────────────────────────────────

def determine_favorite(odds: MatchOdds) -> tuple[str, float]:
    """Maç öncesi favoriyi ve oranını döndürür."""
    if odds.home_win <= odds.away_win:
        return "home", odds.home_win
    return "away", odds.away_win


def check_signal(match: TrackedMatch, live_odds: MatchOdds) -> Optional[dict]:
    """
    HT durumuna ve canlı oranlara göre sinyal üretir.
    Dönüş: sinyal dict ya da None
    """
    hs = match.ht_score_home
    as_ = match.ht_score_away

    if match.favorite == "home":
        fav_goals    = hs
        under_goals  = as_
        fav_live_odds   = live_odds.home_win
        under_live_odds = live_odds.away_win
        fav_name   = match.home_team
        under_name = match.away_team
    else:
        fav_goals    = as_
        under_goals  = hs
        fav_live_odds   = live_odds.away_win
        under_live_odds = live_odds.home_win
        fav_name   = match.away_team
        under_name = match.home_team

    goal_diff = under_goals - fav_goals  # pozitif = favori geride

    if goal_diff == 1 and 0 < fav_live_odds < HT_1GOAL_WIN_THRESHOLD:
        return {
            "type": "1_GOAL_BEHIND",
            "emoji": "🟡",
            "label": "1 GOL GERİDE — Favori Geri Dönebilir",
            "detail": (
                f"Favori *{fav_name}* 1 gol geride\n"
                f"Canlı kazanma oranı: *{fav_live_odds:.2f}* (< {HT_1GOAL_WIN_THRESHOLD})\n"
                f"HT Skor: {match.home_team} {hs}–{as_} {match.away_team}"
            ),
        }

    if goal_diff == 2 and under_live_odds > HT_2GOAL_UNDERDOG_THRESHOLD:
        return {
            "type": "2_GOAL_BEHIND",
            "emoji": "🔴",
            "label": "2 GOL GERİDE — Underdog Tutuyor",
            "detail": (
                f"Favori *{fav_name}* 2 gol geride\n"
                f"*{under_name}* oranı: *{under_live_odds:.2f}* (> {HT_2GOAL_UNDERDOG_THRESHOLD})\n"
                f"HT Skor: {match.home_team} {hs}–{as_} {match.away_team}"
            ),
        }

    return None


# ─── Ana Tarama Döngüsü ────────────────────────────────────────────────────────

async def scan_prematch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bugünkü maçları tarar. Favorinin oranı < 1.60 olanları takip listesine ekler.
    Her ~15 dakikada çalışır (maç başlamadan önce).
    """
    logger.info("Maç öncesi tarama başlıyor...")
    try:
        fixtures = await get_todays_fixtures()
        for f in fixtures:
            fid    = f["fixture"]["id"]
            status = f["fixture"]["status"]["short"]

            if status not in ("NS",):  # sadece başlamamış maçlar
                continue
            if fid in tracked:
                continue

            odds = await get_prematch_odds(fid)
            if not odds or odds.home_win == 0 or odds.away_win == 0:
                continue

            fav, fav_odds = determine_favorite(odds)
            if fav_odds >= PRE_MATCH_FAV_THRESHOLD:
                continue

            # Takip listesine ekle
            tracked[fid] = TrackedMatch(
                fixture_id      = fid,
                home_team       = f["teams"]["home"]["name"],
                away_team       = f["teams"]["away"]["name"],
                league          = f["league"]["name"],
                country         = f["league"]["country"],
                favorite        = fav,
                pre_match_fav_odds = fav_odds,
            )
            logger.info(f"Takibe alındı: {tracked[fid].home_team} vs {tracked[fid].away_team} | Fav oranı: {fav_odds}")

    except Exception as e:
        logger.error(f"scan_prematch hatası: {e}")


async def _evaluate_and_send(match, f, window_label, subscribers, context) -> None:
    """Skor + oran kontrolü yapar, koşullar tutuyorsa sinyal gönderir."""
    hs  = f["goals"]["home"] or 0
    as_ = f["goals"]["away"] or 0
    match.ht_score_home = hs
    match.ht_score_away = as_

    # Favori geride mi?
    if match.favorite == "home":
        behind = as_ > hs
    else:
        behind = hs > as_

    if not behind:
        return

    # Canlı oranları çek
    live_odds = await get_live_odds_odds_api(match.home_team, match.away_team)
    if not live_odds:
        logger.warning(f"Canlı oran bulunamadı: {match.home_team} vs {match.away_team}")
        return

    signal = check_signal(match, live_odds)
    if not signal:
        return

    match.signal_sent = True
    elapsed = f["fixture"]["status"].get("elapsed", "")
    time_str = f"dk {elapsed}" if elapsed else window_label

    msg = (
        f"{signal['emoji']} *{signal['label']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 {match.league} ({match.country})\n"
        f"{signal['detail']}\n"
        f"📊 Maç öncesi favori oranı: *{match.pre_match_fav_odds:.2f}*\n"
        f"⏱ Pencere: *{window_label}* | 🕐 {datetime.now().strftime('%H:%M')}"
    )
    for chat_id in subscribers:
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=msg, parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Mesaj gönderilemedi {chat_id}: {e}")


async def scan_halftime(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    İki pencereyi kontrol eder:
      • HT (İlk yarı molası)
      • 50–55. dakika (2. yarı aktif oyun)
    Hangi pencere önce tetiklenirse o sinyal gönderilir, ikincisi atlanır.
    Her 2 dakikada çalışır.
    """
    if not tracked:
        return

    subscribers: set = context.bot_data.get("subscribers", set())
    if not subscribers:
        return

    try:
        live_fixtures = await get_live_fixtures()

        # Pencere 1: HT molası
        ht_map = {
            f["fixture"]["id"]: f
            for f in live_fixtures
            if f["fixture"]["status"]["short"] == "HT"
        }

        # Pencere 2: 50–55. dakika (2H status, aktif oyun)
        minute_map = {
            f["fixture"]["id"]: f
            for f in live_fixtures
            if f["fixture"]["status"]["short"] == "2H"
            and MINUTE_WINDOW_START <= (f["fixture"]["status"].get("elapsed") or 0) <= MINUTE_WINDOW_END
        }

        for fid, match in list(tracked.items()):
            if match.signal_sent:
                continue

            if fid in ht_map:
                await _evaluate_and_send(match, ht_map[fid], "HT Molası", subscribers, context)

            # HT sinyali gönderildiyse 50-55 penceresini atla
            if match.signal_sent:
                continue

            if fid in minute_map:
                elapsed = minute_map[fid]["fixture"]["status"].get("elapsed", "")
                await _evaluate_and_send(match, minute_map[fid], f"dk {elapsed} (50–55)", subscribers, context)

    except Exception as e:
        logger.error(f"scan_halftime hatası: {e}")


# ─── Komutlar ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers: set = context.bot_data.setdefault("subscribers", set())
    subscribers.add(update.effective_chat.id)
    await update.message.reply_text(
        "⚽ *Akıllı Maç Filtresi Botu*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Sistem otomatik olarak şu kriterlere uyan maçları bulur:\n\n"
        "✅ Maç öncesi favori oranı *< 1.60*\n"
        "✅ Favori ilk yarıyı *geride bitirir*\n"
        "🟡 *1 fark* → Favorinin FT kazanma oranı *< 2.30*\n"
        "🔴 *2 fark* → Underdogun FT kazanma oranı *> 1.90*\n\n"
        "Bildirimler otomatik açıldı! Sinyal gelince seni haberdar ederim.\n\n"
        "• /status — Takipteki maçları gör\n"
        "• /stop — Bildirimleri kapat",
        parse_mode="Markdown",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not tracked:
        await update.message.reply_text("📋 Şu an takipte maç yok.")
        return

    lines = ["📋 *Takipteki Maçlar*\n"]
    for m in tracked.values():
        fav_side = "Ev" if m.favorite == "home" else "Dep"
        sent = "✅ Sinyal gönderildi" if m.signal_sent else "⏳ Bekleniyor"
        lines.append(
            f"• *{m.home_team}* vs *{m.away_team}*\n"
            f"  {m.league} | Fav({fav_side}): {m.pre_match_fav_odds:.2f} | {sent}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs: set = context.bot_data.get("subscribers", set())
    subs.discard(update.effective_chat.id)
    await update.message.reply_text("🔕 Bildirimler kapatıldı. Tekrar açmak için /start yaz.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *Komutlar*\n\n"
        "• /start — Botu başlat & bildirimleri aç\n"
        "• /status — Bugün takipteki maçları listele\n"
        "• /stop — Bildirimleri kapat\n"
        "• /help — Bu menü\n\n"
        "Bot her 15 dakikada maç öncesi oranları tarar,\n"
        "her 2 dakikada HT sinyali kontrol eder.",
        parse_mode="Markdown",
    )


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    for key, name in [
        (TELEGRAM_TOKEN, "TELEGRAM_TOKEN"),
        (FOOTBALL_API_KEY, "FOOTBALL_API_KEY"),
        (ODDS_API_KEY, "ODDS_API_KEY"),
    ]:
        if not key:
            raise ValueError(f"{name} .env dosyasında bulunamadı!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("stop",   stop_command))
    app.add_handler(CommandHandler("help",   help_command))

    jq = app.job_queue
    jq.run_repeating(scan_prematch, interval=900, first=10)
    jq.run_repeating(scan_halftime, interval=120, first=30)

    logger.info("⚽ Akıllı Maç Filtresi başlatıldı!")

    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot çalışıyor, durdurmak için Ctrl+C bas.")
        # Sonsuza kadar çalış
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
