"""
⚽ Akıllı Maç Filtresi v2 — Telegram Botu
  1. Saatte 1 kez (07:00-24:00 TR) → sonraki 60 dk maçları tara, favori < 1.60 → takibe al
  2. 35. dk'dan itibaren her 5 dk + HT kontrol:
       • Favori öne geçti → TAKİPTEN ÇIK + bildirim
       • 1 gol fark → favori FT < 2.40  → 🟡
       • 2 gol fark → underdog FT > 1.80 → 🔴
       • 3 gol fark → lider FT > 1.15   → 🔵
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
ODDS_API_KEY     = os.getenv("ODDS_API_KEY")

FOOTBALL_BASE = "https://v3.football.api-sports.io"
ODDS_BASE     = "https://api.the-odds-api.com/v4"

PRE_MATCH_FAV_MAX     = 1.60
SCAN_FROM_MINUTE      = 35
SCAN_INTERVAL_MINUTES = 5
FAV_1GOAL_MAX         = 2.40
UNDER_2GOAL_MIN       = 1.80
LEADER_3GOAL_MIN      = 1.15
TR_OFFSET             = timedelta(hours=3)
ACTIVE_START          = 7
ACTIVE_END            = 24

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def tr_now() -> datetime:
    return datetime.now(timezone.utc) + TR_OFFSET


def is_active_hours() -> bool:
    return ACTIVE_START <= tr_now().hour < ACTIVE_END


@dataclass
class MatchOdds:
    home_win: float = 0.0
    draw: float     = 0.0
    away_win: float = 0.0


@dataclass
class TrackedMatch:
    fixture_id:          int
    home_team:           str
    away_team:           str
    league:              str
    country:             str
    favorite:            str
    pre_fav_odds:        float
    signaled_diffs:      set  = field(default_factory=set)
    last_checked_minute: int  = 0
    drop_notified:       bool = False


tracked: dict = {}


async def get_todays_fixtures() -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{FOOTBALL_BASE}/fixtures?date={today}", headers=headers) as r:
            return (await r.json()).get("response", [])


async def get_live_fixtures() -> list:
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{FOOTBALL_BASE}/fixtures?live=all", headers=headers) as r:
            return (await r.json()).get("response", [])


async def get_prematch_odds(fixture_id: int) -> Optional[MatchOdds]:
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{FOOTBALL_BASE}/odds?fixture={fixture_id}&bet=1", headers=headers) as r:
            resp = (await r.json()).get("response", [])
            if not resp:
                return None
            try:
                values = resp[0]["bookmakers"][0]["bets"][0]["values"]
                odds = MatchOdds()
                for v in values:
                    if v["value"] == "Home":   odds.home_win = float(v["odd"])
                    elif v["value"] == "Draw": odds.draw     = float(v["odd"])
                    elif v["value"] == "Away": odds.away_win = float(v["odd"])
                return odds
            except:
                return None


async def get_live_odds(home: str, away: str) -> Optional[MatchOdds]:
    params = {"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{ODDS_BASE}/sports/soccer/odds", params=params) as r:
                if r.status != 200:
                    return None
                data = await r.json()
    except Exception as e:
        logger.error(f"Odds API: {e}")
        return None

    hl, al = home.lower(), away.lower()
    for event in data:
        eh = event.get("home_team", "").lower()
        ea = event.get("away_team", "").lower()
        if not ((hl in eh or eh in hl) and (al in ea or ea in al)):
            continue
        for bm in event.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                odds = MatchOdds()
                for o in mkt["outcomes"]:
                    n = o["name"].lower()
                    if "draw" in n:          odds.draw     = float(o["price"])
                    elif hl in n or n in hl: odds.home_win = float(o["price"])
                    else:                    odds.away_win = float(o["price"])
                if odds.home_win and odds.away_win:
                    return odds
    return None


def check_signal(match, live_odds, home_g, away_g):
    if match.favorite == "home":
        fav_g, und_g = home_g, away_g
        fav_ft, und_ft = live_odds.home_win, live_odds.away_win
        fav_name, und_name = match.home_team, match.away_team
    else:
        fav_g, und_g = away_g, home_g
        fav_ft, und_ft = live_odds.away_win, live_odds.home_win
        fav_name, und_name = match.away_team, match.home_team

    diff = und_g - fav_g
    skor = f"{match.home_team} {home_g}–{away_g} {match.away_team}"

    if diff == 1 and 1 not in match.signaled_diffs and 0 < fav_ft < FAV_1GOAL_MAX:
        return {"diff": 1, "emoji": "🟡", "label": "1 GOL GERİDE — Favori Geri Dönebilir",
                "detail": f"Favori *{fav_name}* 1 gol geride\nCanlı kazanma: *{fav_ft:.2f}* (< {FAV_1GOAL_MAX})\nSkor: {skor}"}

    if diff == 2 and 2 not in match.signaled_diffs and und_ft > UNDER_2GOAL_MIN:
        return {"diff": 2, "emoji": "🔴", "label": "2 GOL GERİDE — Underdog Tutuyor",
                "detail": f"Favori *{fav_name}* 2 gol geride\n*{und_name}* oranı: *{und_ft:.2f}* (> {UNDER_2GOAL_MIN})\nSkor: {skor}"}

    if diff == 3 and 3 not in match.signaled_diffs and und_ft > LEADER_3GOAL_MIN:
        return {"diff": 3, "emoji": "🔵", "label": "3 GOL GERİDE — Lider Güçlü",
                "detail": f"Favori *{fav_name}* 3 gol geride\n*{und_name}* oranı: *{und_ft:.2f}* (> {LEADER_3GOAL_MIN})\nSkor: {skor}"}

    return None


async def scan_prematch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saatte 1 kez çalışır (07:00-24:00 TR). Sonraki 60 dk maçları tarar."""
    if not is_active_hours():
        return
    try:
        fixtures = await get_todays_fixtures()
        now_utc  = datetime.now(timezone.utc)
        new_count = 0

        for f in fixtures:
            fid = f["fixture"]["id"]
            if f["fixture"]["status"]["short"] != "NS" or fid in tracked:
                continue
            try:
                kick = datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00"))
                mins = (kick - now_utc).total_seconds() / 60
                if not (0 <= mins <= 70):
                    continue
            except:
                continue

            odds = await get_prematch_odds(fid)
            if not odds or not odds.home_win or not odds.away_win:
                continue

            if odds.home_win < odds.away_win and odds.home_win < PRE_MATCH_FAV_MAX:
                fav, fav_odds = "home", odds.home_win
            elif odds.away_win < odds.home_win and odds.away_win < PRE_MATCH_FAV_MAX:
                fav, fav_odds = "away", odds.away_win
            else:
                continue

            tracked[fid] = TrackedMatch(
                fixture_id=fid,
                home_team=f["teams"]["home"]["name"],
                away_team=f["teams"]["away"]["name"],
                league=f["league"]["name"],
                country=f["league"]["country"],
                favorite=fav,
                pre_fav_odds=fav_odds,
            )
            new_count += 1
            logger.info(f"Takibe: {tracked[fid].home_team} vs {tracked[fid].away_team} | {fav_odds:.2f}")

        logger.info(f"Saatlik tarama ({tr_now().strftime('%H:%M')} TR): {new_count} yeni maç.")
    except Exception as e:
        logger.error(f"scan_prematch: {e}")


async def scan_live(context: ContextTypes.DEFAULT_TYPE) -> None:
    """07:00-24:00 TR arası her 5 dk + HT sinyal kontrolü."""
    if not is_active_hours():
        return
    if not tracked:
        return
    subscribers: set = context.bot_data.get("subscribers", set())
    if not subscribers:
        return

    try:
        live_map = {f["fixture"]["id"]: f for f in await get_live_fixtures()}

        for fid, match in list(tracked.items()):
            f = live_map.get(fid)
            if not f:
                continue

            status  = f["fixture"]["status"]["short"]
            elapsed = f["fixture"]["status"].get("elapsed") or 0
            home_g  = f["goals"]["home"] or 0
            away_g  = f["goals"]["away"] or 0

            is_ht     = status == "HT"
            is_active = status in ("1H", "2H", "ET") and elapsed >= SCAN_FROM_MINUTE

            if not is_ht and not is_active:
                continue
            if is_active and elapsed - match.last_checked_minute < SCAN_INTERVAL_MINUTES:
                continue

            match.last_checked_minute = elapsed

            if match.favorite == "home":
                fav_g, und_g, fav_name = home_g, away_g, match.home_team
            else:
                fav_g, und_g, fav_name = away_g, home_g, match.away_team

            # Favori öne geçti → takipten çıkar + bildirim
            if fav_g > und_g and not match.drop_notified:
                match.drop_notified = True
                tracked.pop(fid, None)
                msg = (
                    f"✅ *TAKİPTEN ÇIKTI*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏆 {match.league} ({match.country})\n"
                    f"⚽ *{match.home_team}* {home_g}–{away_g} *{match.away_team}*\n"
                    f"Favori *{fav_name}* öne geçti → takip sonlandırıldı\n"
                    f"⏱ dk {elapsed} | 🕐 {tr_now().strftime('%H:%M')} TR"
                )
                for chat_id in subscribers:
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    except Exception as e:
                        logger.warning(f"Drop: {e}")
                continue

            if fav_g >= und_g:
                continue

            live_odds = await get_live_odds(match.home_team, match.away_team)
            if not live_odds:
                continue

            signal = check_signal(match, live_odds, home_g, away_g)
            if not signal:
                continue

            match.signaled_diffs.add(signal["diff"])
            window = "HT" if is_ht else f"dk {elapsed}"
            msg = (
                f"{signal['emoji']} *{signal['label']}*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏆 {match.league} ({match.country})\n"
                f"{signal['detail']}\n"
                f"📊 Maç öncesi favori: *{match.pre_fav_odds:.2f}*\n"
                f"⏱ Pencere: *{window}* | 🕐 {tr_now().strftime('%H:%M')} TR"
            )
            for chat_id in subscribers:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                except Exception as e:
                    logger.warning(f"Sinyal: {e}")

    except Exception as e:
        logger.error(f"scan_live: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data.setdefault("subscribers", set()).add(update.effective_chat.id)
    await update.message.reply_text(
        "⚽ *Akıllı Maç Filtresi v2*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Favori < *1.60* → saatte 1 kez taranır (07:00-24:00 TR)\n"
        "✅ 35. dk'dan her 5 dk + HT kontrol\n\n"
        "🟡 *1 fark* → favori FT < 2.40\n"
        "🔴 *2 fark* → underdog FT > 1.80\n"
        "🔵 *3 fark* → lider FT > 1.15\n"
        "✅ Favori öne geçince → takipten çıkar\n\n"
        "• /status — Takipteki maçlar\n"
        "• /stop — Bildirimleri kapat",
        parse_mode="Markdown",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not tracked:
        await update.message.reply_text("📋 Şu an takipte maç yok.")
        return
    lines = ["📋 *Takipteki Maçlar*\n"]
    for m in tracked.values():
        durum = f"Sinyaller: {m.signaled_diffs}" if m.signaled_diffs else "⏳ Bekleniyor"
        lines.append(f"• *{m.home_team}* vs *{m.away_team}* | {m.league} | {durum}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data.get("subscribers", set()).discard(update.effective_chat.id)
    await update.message.reply_text("🔕 Bildirimler kapatıldı. /start ile tekrar aç.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📖 *Komutlar*\n\n/start /status /stop /help", parse_mode="Markdown")


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

    # Saatte 1 kez prematch tarama
    app.job_queue.run_repeating(scan_prematch, interval=3600, first=10)
    # Her 5 dk canlı tarama
    app.job_queue.run_repeating(scan_live, interval=300, first=60)

    logger.info("⚽ Akıllı Maç Filtresi v2 başlatıldı!")
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot çalışıyor.")
        await asyncio.Event().wait()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
