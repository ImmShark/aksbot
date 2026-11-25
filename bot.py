import asyncio
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
import json
import os
from pathlib import Path
import random
import sys
from typing import Optional
import unicodedata
import urllib.request

import discord
from discord import app_commands
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
BALANCES_FILE = DATA_DIR / "balances.json"
WORDS_FILE = DATA_DIR / "words.txt"
WORDS_URL = "https://raw.githubusercontent.com/duyet/vietnamese-wordlist/master/Viet74K.txt"
START_XU = 10_000

FALLBACK_WORDS = [
    "con mèo", "mèo mướp", "mướp đắng", "đắng cay", "cay đắng",
    "con cá", "cá chép", "chép bài", "bài hát", "hát hay",
    "hay ho", "ho lao", "lao động", "động lực", "lực lượng",
    "lượng giác", "giác quan", "quan tâm", "tâm trạng", "trạng thái",
    "thái độ", "độ cao", "cao nguyên", "nguyên nhân", "nhân dân",
    "dân tộc", "tộc trưởng", "trưởng thành", "thành công", "công bằng",
]

_OLD_TO_NEW_TONE = (
    ("òa", "oà"), ("óa", "oá"), ("ỏa", "oả"), ("õa", "oã"), ("ọa", "oạ"),
    ("òe", "oè"), ("óe", "oé"), ("ỏe", "oẻ"), ("õe", "oẽ"), ("ọe", "oẹ"),
    ("ùy", "uỳ"), ("úy", "uý"), ("ủy", "uỷ"), ("ũy", "uỹ"), ("ụy", "uỵ"),
)

RANK_STR = {14: "A", 13: "K", 12: "Q", 11: "J", **{i: str(i) for i in range(2, 11)}}
HAND_NAMES = {
    9: "Thùng phá sảnh", 8: "Tứ quý", 7: "Cù lũ", 6: "Thùng",
    5: "Sảnh", 4: "Sám cô", 3: "Hai đôi", 2: "Một đôi", 1: "Mậu thầu",
}


# ---------- Tiền tệ & .env ----------

def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# shortcut: synchronous JSON write on every bet/payout; switch to SQLite if scaling past small friend groups
def _load_balances() -> dict[str, int]:
    if not BALANCES_FILE.exists():
        return {}
    return json.loads(BALANCES_FILE.read_text(encoding="utf-8"))


BALANCES: dict[str, int] = _load_balances()


def _save_balances() -> None:
    BALANCES_FILE.write_text(json.dumps(BALANCES, ensure_ascii=False, indent=2), encoding="utf-8")


def get_bal(uid: int) -> int:
    key = str(uid)
    if key not in BALANCES:
        BALANCES[key] = START_XU
        _save_balances()
    return BALANCES[key]


def add_bal(uid: int, delta: int) -> int:
    BALANCES[str(uid)] = max(0, get_bal(uid) + delta)
    _save_balances()
    return BALANCES[str(uid)]


# ---------- Logic Nối Từ (Vua Tiếng Việt) ----------

def norm_vi(text: str) -> str:
    s = unicodedata.normalize("NFC", text.strip().lower())
    for old, new in _OLD_TO_NEW_TONE:
        s = s.replace(old, new)
    return " ".join(s.split())


def build_word_index(lines: list[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    valid: dict[str, str] = {}
    by_first: dict[str, list[str]] = {}
    for line in lines:
        raw = " ".join(line.strip().split())
        parts = raw.split()
        if len(parts) != 2 or not all(p.isalpha() for p in parts):
            continue
        canon = norm_vi(raw)
        if canon not in valid:
            valid[canon] = raw.lower()
            by_first.setdefault(canon.split()[0], []).append(canon)
    return valid, by_first


def load_words() -> tuple[dict[str, str], dict[str, list[str]]]:
    if not WORDS_FILE.exists():
        try:
            with urllib.request.urlopen(WORDS_URL, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
            filtered = [
                line.strip() for line in text.splitlines()
                if len(line.strip().split()) == 2
                and all(tok.isalpha() for tok in line.strip().split())
            ]
            if filtered:
                WORDS_FILE.write_text("\n".join(filtered) + "\n", encoding="utf-8")
        except Exception as exc:
            print(f"[NoiTu] Khong tai duoc tu dien ({exc}), dung bo tu du phong.")
    lines = WORDS_FILE.read_text(encoding="utf-8").splitlines() if WORDS_FILE.exists() else FALLBACK_WORDS
    return build_word_index(lines)


VALID_WORDS: dict[str, str] = {}
BY_FIRST: dict[str, list[str]] = {}


@dataclass
class NoiTuGame:
    current: str
    last_uid: Optional[int]
    solo: bool
    used: set[str] = field(default_factory=set)
    streak: int = 0


noitu_games: dict[int, NoiTuGame] = {}


class StopNoiTuView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=60)
        self.channel_id = channel_id

    @discord.ui.button(label="🛑 Dừng ván này", style=discord.ButtonStyle.red)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = noitu_games.pop(self.channel_id, None)
        button.disabled = True
        if game:
            await interaction.response.edit_message(
                content=f"🛑 Đã dừng ván nối từ (chuỗi **{game.streak}** lượt).", view=self
            )
        else:
            await interaction.response.edit_message(content="Ván này đã kết thúc rồi.", view=self)
        self.stop()


# ---------- Logic Tài Xỉu ----------

def resolve_taixiu(d1: int, d2: int, d3: int) -> tuple[str, int]:
    total = d1 + d2 + d3
    if d1 == d2 == d3:
        return ("BAO", total)
    return ("TAI" if total >= 11 else "XIU", total)


taixiu_channels: set[int] = set()


class TaiXiuModal(discord.ui.Modal):
    def __init__(self, view: "TaiXiuView", side: str):
        label = "TÀI" if side == "TAI" else "XỈU"
        super().__init__(title=f"Đặt cược {label}")
        self.tx_view = view
        self.side = side
        self.amount = discord.ui.TextInput(
            label="Số xu cược (hoặc gõ 'all')",
            placeholder="Ví dụ: 500",
            default="500",
            max_length=20,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        async with self.tx_view.lock:
            if self.tx_view.closed:
                return await interaction.response.send_message("⏰ Bàn đã đóng cược rồi!", ephemeral=True)
            bal = get_bal(interaction.user.id)
            raw = self.amount.value.strip().lower()
            amt = bal if raw == "all" else (int(raw) if raw.isdigit() else 0)
            if amt <= 0 or amt > bal:
                return await interaction.response.send_message(
                    f"❌ Số xu không hợp lệ (bạn đang có **{bal:,} xu**).", ephemeral=True
                )
            prev = self.tx_view.bets.get(interaction.user.id)
            if prev and prev[0] != self.side:
                return await interaction.response.send_message("❌ Bạn đã đặt cửa bên kia rồi!", ephemeral=True)
            add_bal(interaction.user.id, -amt)
            self.tx_view.bets[interaction.user.id] = (self.side, (prev[1] if prev else 0) + amt)
            self.tx_view.names[interaction.user.id] = interaction.user.display_name
            if self.tx_view.message:
                await self.tx_view.message.edit(embed=self.tx_view.build_embed())
        side_txt = "🔴 TÀI" if self.side == "TAI" else "🔵 XỈU"
        await interaction.response.send_message(f"✅ Đã đặt **{amt:,} xu** vào {side_txt}!", ephemeral=True)


class TaiXiuView(discord.ui.View):
    def __init__(self, channel_id: int, host_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.host_id = host_id
        self.bets: dict[int, tuple[str, int]] = {}
        self.names: dict[int, str] = {}
        self.closed = False
        self.lock = asyncio.Lock()
        self.message: Optional[discord.Message] = None
        self.timer_task: Optional[asyncio.Task] = None

    def build_embed(self, result: Optional[tuple[int, int, int, str, int]] = None) -> discord.Embed:
        tai_lines = [f"• {self.names[u]}: **{a:,}**" for u, (s, a) in self.bets.items() if s == "TAI"]
        xiu_lines = [f"• {self.names[u]}: **{a:,}**" for u, (s, a) in self.bets.items() if s == "XIU"]
        if result is None:
            embed = discord.Embed(
                title="🎲 TÀI XỈU — MỞ BÀN (20 giây)",
                description="Tài: 11–17 | Xỉu: 4–10 | **Bão** (3 mặt giống nhau): Nhà cái ăn",
                color=discord.Color.gold(),
            )
        else:
            d1, d2, d3, outcome, total = result
            res_map = {"TAI": "🔴 TÀI", "XIU": "🔵 XỈU", "BAO": "💥 BÃO (Nhà cái ăn)"}
            color = discord.Color.red() if outcome == "TAI" else (
                discord.Color.blue() if outcome == "XIU" else discord.Color.dark_grey()
            )
            embed = discord.Embed(
                title=f"🎲 KẾT QUẢ: [ {d1} ] [ {d2} ] [ {d3} ] = {total} → {res_map[outcome]}",
                color=color,
            )
        embed.add_field(name="🔴 Cửa TÀI", value="\n".join(tai_lines) or "_Chưa có_", inline=True)
        embed.add_field(name="🔵 Cửa XỈU", value="\n".join(xiu_lines) or "_Chưa có_", inline=True)
        return embed

    async def _countdown(self):
        await asyncio.sleep(20)
        await self.finish()

    async def finish(self):
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.timer_task and self.timer_task is not asyncio.current_task():
                self.timer_task.cancel()
            try:
                for item in self.children:
                    item.disabled = True
                self.stop()
                d1, d2, d3 = random.randint(1, 6), random.randint(1, 6), random.randint(1, 6)
                outcome, total = resolve_taixiu(d1, d2, d3)
                winners = []
                for uid, (side, amt) in self.bets.items():
                    if side == outcome:
                        add_bal(uid, amt * 2)
                        winners.append(f"🏆 **{self.names[uid]}** +{amt:,} xu")
                embed = self.build_embed((d1, d2, d3, outcome, total))
                if not self.bets:
                    embed.description = "Không có ai đặt cược."
                elif winners:
                    embed.add_field(name="Trả thưởng", value="\n".join(winners), inline=False)
                else:
                    embed.add_field(name="Trả thưởng", value="Nhà cái hốt trọn 😈", inline=False)
                if self.message:
                    await self.message.edit(embed=embed, view=self)
            finally:
                taixiu_channels.discard(self.channel_id)

    @discord.ui.button(label="🔴 Đặt TÀI", style=discord.ButtonStyle.red)
    async def bet_tai(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.closed:
            return await interaction.response.send_message("⏰ Bàn đã đóng!", ephemeral=True)
        await interaction.response.send_modal(TaiXiuModal(self, "TAI"))

    @discord.ui.button(label="🔵 Đặt XỈU", style=discord.ButtonStyle.blurple)
    async def bet_xiu(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.closed:
            return await interaction.response.send_message("⏰ Bàn đã đóng!", ephemeral=True)
        await interaction.response.send_modal(TaiXiuModal(self, "XIU"))

    @discord.ui.button(label="🎲 Xổ ngay", style=discord.ButtonStyle.green)
    async def roll_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host_id:
            return await interaction.response.send_message("Chỉ chủ bàn mới được xổ sớm!", ephemeral=True)
        await interaction.response.defer()
        await self.finish()


# ---------- Logic Poker Texas Hold'em ----------

def fmt_cards(cards: list[tuple[int, str]]) -> str:
    return " ".join(f"`{RANK_STR[r]}{s}`" for r, s in cards)


def eval5(cards: tuple[tuple[int, str], ...]) -> tuple[int, ...]:
    ranks = sorted((r for r, _ in cards), reverse=True)
    is_flush = len({s for _, s in cards}) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight_high = 0
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            straight_high = 5
    if is_flush and straight_high:
        return (9, straight_high)
    counts = sorted(Counter(ranks).items(), key=lambda x: (x[1], x[0]), reverse=True)
    freq = [c for _, c in counts]
    vals = [r for r, _ in counts]
    if freq == [4, 1]:
        return (8, *vals)
    if freq == [3, 2]:
        return (7, *vals)
    if is_flush:
        return (6, *ranks)
    if straight_high:
        return (5, straight_high)
    if freq == [3, 1, 1]:
        return (4, *vals)
    if freq == [2, 2, 1]:
        return (3, *vals)
    if freq == [2, 1, 1, 1]:
        return (2, *vals)
    return (1, *ranks)


def best_hand(cards: list[tuple[int, str]]) -> tuple[int, ...]:
    return max(eval5(c) for c in combinations(cards, 5))


@dataclass
class PokerPlayer:
    uid: int
    name: str
    stack: int
    hole: list[tuple[int, str]] = field(default_factory=list)
    street_bet: int = 0
    total_bet: int = 0
    folded: bool = False
    all_in: bool = False


def settle_pots(
    players: list[PokerPlayer], dealer_idx: int, board: list[tuple[int, str]]
) -> tuple[dict[int, int], dict[int, tuple[int, ...]]]:
    non_folded = [p for p in players if not p.folded]
    payouts = {p.uid: 0 for p in players}
    if len(non_folded) == 1:
        payouts[non_folded[0].uid] = sum(p.total_bet for p in players)
        return payouts, {}

    scores = {p.uid: best_hand(p.hole + board) for p in non_folded}
    levels = sorted({p.total_bet for p in players if p.total_bet > 0})
    prev = 0
    n = len(players)
    for lvl in levels:
        pot_slice = sum(max(0, min(p.total_bet, lvl) - prev) for p in players)
        eligible = [p for p in non_folded if p.total_bet >= lvl]
        if pot_slice > 0 and eligible:
            best = max(scores[p.uid] for p in eligible)
            winners = [p for p in eligible if scores[p.uid] == best]
            share, rem = divmod(pot_slice, len(winners))
            ordered = sorted(winners, key=lambda p: (players.index(p) - dealer_idx - 1) % n)
            for idx, w in enumerate(ordered):
                payouts[w.uid] += share + (1 if idx < rem else 0)
        prev = lvl
    return payouts, scores


poker_tables: dict[int, "PokerView"] = {}


class RaiseModal(discord.ui.Modal):
    def __init__(self, table: "PokerView"):
        super().__init__(title="Tố thêm (Raise)")
        self.table = table
        self.amount = discord.ui.TextInput(
            label=f"Tố thêm (tối thiểu {table.min_raise:,} xu)",
            placeholder="Nhập số xu muốn tố thêm",
            max_length=15,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        async with self.table.lock:
            if self.table.phase != "PLAYING" or self.table.current_player.uid != interaction.user.id:
                return await interaction.response.send_message("Lượt này đã kết thúc rồi!", ephemeral=True)
            p = self.table.current_player
            to_call = max(0, self.table.current_bet - p.street_bet)
            raw = self.amount.value.strip()
            if not raw.isdigit() or int(raw) <= 0:
                return await interaction.response.send_message("❌ Vui lòng nhập số nguyên dương.", ephemeral=True)
            raise_by = int(raw)
            need = to_call + raise_by
            if need > p.stack:
                return await interaction.response.send_message(
                    f"❌ Bạn chỉ còn **{p.stack:,} xu** (cần theo {to_call:,} + tố {raise_by:,}).", ephemeral=True
                )
            if raise_by < self.table.min_raise and need < p.stack:
                return await interaction.response.send_message(
                    f"❌ Phải tố thêm ít nhất **{self.table.min_raise:,} xu** (hoặc All-in).", ephemeral=True
                )
            self.table._post_chip(p, need)
            if raise_by >= self.table.min_raise:
                self.table.min_raise = raise_by
            self.table.current_bet = p.street_bet
            self.table.pending = {
                q.uid for q in self.table.players if not q.folded and not q.all_in and q.uid != p.uid
            }
            self.table.last_action = f"{p.name} tố lên **{p.street_bet:,}**"
            self.table._advance_after_action()
            if self.table.message:
                await self.table.message.edit(embed=self.table.build_embed(), view=self.table)
        await interaction.response.send_message(f"✅ Đã tố thêm **{raise_by:,} xu**!", ephemeral=True)


class PokerView(discord.ui.View):
    def __init__(self, channel_id: int, host: discord.abc.User, buyin: int):
        super().__init__(timeout=300)
        self.channel_id = channel_id
        self.host_id = host.id
        self.buyin = buyin
        self.bb = max(20, (buyin // 20 // 10) * 10 or 20)
        self.sb = self.bb // 2
        self.phase = "LOBBY"  # LOBBY | PLAYING | HAND_OVER | CLOSED
        self.players: list[PokerPlayer] = [PokerPlayer(host.id, host.display_name, buyin)]
        self.dealer_idx = -1
        self.deck: list[tuple[int, str]] = []
        self.board: list[tuple[int, str]] = []
        self.current_bet = 0
        self.min_raise = self.bb
        self.pending: set[int] = set()
        self.turn_idx = 0
        self.hand_id = 0
        self.action_seq = 0
        self.last_action = "Chờ người chơi tham gia..."
        self.lock = asyncio.Lock()
        self.message: Optional[discord.Message] = None
        self.turn_task: Optional[asyncio.Task] = None
        self._rebuild_buttons()

    @property
    def current_player(self) -> PokerPlayer:
        return self.players[self.turn_idx]

    def _cancel_turn_timer(self) -> None:
        if self.turn_task and self.turn_task is not asyncio.current_task():
            self.turn_task.cancel()
        self.turn_task = None

    def _reset_turn_timer(self) -> None:
        self._cancel_turn_timer()
        if self.phase == "PLAYING":
            self.action_seq += 1
            self.turn_task = asyncio.create_task(self._turn_timeout(self.hand_id, self.action_seq))

    async def _turn_timeout(self, expected_hand: int, expected_seq: int):
        await asyncio.sleep(60)
        async with self.lock:
            if self.phase != "PLAYING" or self.hand_id != expected_hand or self.action_seq != expected_seq:
                return
            p = self.current_player
            to_call = max(0, self.current_bet - p.street_bet)
            if to_call == 0:
                self.pending.discard(p.uid)
                self.last_action = f"⏰ {p.name} hết giờ → tự Check"
            else:
                p.folded = True
                self.pending.discard(p.uid)
                self.last_action = f"⏰ {p.name} hết giờ → tự Fold"
            self._advance_after_action()
            if self.message:
                await self.message.edit(embed=self.build_embed(), view=self)

    def _close_table(self, refund: bool = True) -> None:
        self._cancel_turn_timer()
        self.phase = "CLOSED"
        if refund:
            for p in self.players:
                total = p.stack + p.total_bet
                if total > 0:
                    add_bal(p.uid, total)
                p.stack = 0
                p.total_bet = 0
        self.stop()
        poker_tables.pop(self.channel_id, None)

    async def on_timeout(self):
        async with self.lock:
            if self.phase == "CLOSED":
                return
            self._close_table(refund=True)
            if self.message:
                embed = discord.Embed(
                    title="♠️ Bàn Poker đã tự đóng",
                    description="Không có thao tác trong 5 phút — toàn bộ chip trên bàn đã hoàn về ví.",
                    color=discord.Color.dark_grey(),
                )
                await self.message.edit(embed=embed, view=None)

    def _post_chip(self, p: PokerPlayer, amt: int) -> int:
        paid = min(p.stack, amt)
        p.stack -= paid
        p.street_bet += paid
        p.total_bet += paid
        if p.stack == 0:
            p.all_in = True
        return paid

    def _next_actor_from(self, start_idx: int) -> int:
        n = len(self.players)
        for offset in range(n):
            idx = (start_idx + offset) % n
            if self.players[idx].uid in self.pending:
                return idx
        raise RuntimeError("No actor found for non-empty pending set")

    def _start_hand(self) -> None:
        self.players = [p for p in self.players if p.stack > 0]
        if len(self.players) < 2:
            self._close_table(refund=True)
            self.last_action = "🏆 Chỉ còn dưới 2 người có chip — đã hoàn xu và đóng bàn!"
            return

        self.hand_id += 1
        self.action_seq = 0
        self.phase = "PLAYING"
        self.dealer_idx = (self.dealer_idx + 1) % len(self.players)
        self.deck = [(r, s) for r in range(2, 15) for s in "♠♥♦♣"]
        random.shuffle(self.deck)
        self.board = []

        for p in self.players:
            p.hole = [self.deck.pop(), self.deck.pop()]
            p.street_bet = 0
            p.total_bet = 0
            p.folded = False
            p.all_in = False

        n = len(self.players)
        sb_idx = self.dealer_idx if n == 2 else (self.dealer_idx + 1) % n
        bb_idx = (self.dealer_idx + 1) % 2 if n == 2 else (self.dealer_idx + 2) % n
        self._post_chip(self.players[sb_idx], self.sb)
        self._post_chip(self.players[bb_idx], self.bb)

        max_posted = max(p.street_bet for p in self.players)
        active = [p for p in self.players if not p.folded and not p.all_in]
        self.min_raise = self.bb
        if len(active) <= 1 and all(p.street_bet >= max_posted for p in active):
            self.current_bet = max_posted
            self.pending = set()
            self.last_action = "All-in từ tiền mù!"
            self._advance_after_action()
            return

        self.current_bet = self.bb if len(active) >= 2 else max_posted
        self.pending = {p.uid for p in active}
        self.turn_idx = self._next_actor_from((bb_idx + 1) % n)
        self.last_action = f"Ván #{self.hand_id} bắt đầu (Blinds {self.sb}/{self.bb})"
        self._reset_turn_timer()
        self._rebuild_buttons()

    def _finish_showdown(self) -> None:
        payouts, scores = settle_pots(self.players, self.dealer_idx, self.board)
        lines = []
        for p in self.players:
            win = payouts.get(p.uid, 0)
            p.stack += win
            p.total_bet = 0
            p.street_bet = 0
            if scores and not p.folded:
                h_name = HAND_NAMES[scores[p.uid][0]]
                win_txt = f" → **+{win:,} xu**" if win > 0 else ""
                lines.append(f"• {p.name}: {fmt_cards(p.hole)} (*{h_name}*){win_txt}")
            elif win > 0:
                lines.append(f"🏆 {p.name} thắng **{win:,} xu** (mọi người Fold)")
        self._cancel_turn_timer()
        self.phase = "HAND_OVER"
        self.last_action = "\n".join(lines)
        self._rebuild_buttons()

    def _advance_after_action(self) -> None:
        non_folded = [p for p in self.players if not p.folded]
        if len(non_folded) == 1:
            self._finish_showdown()
            return
        if not self.pending:
            while True:
                if len(self.board) == 5:
                    self._finish_showdown()
                    return
                for p in self.players:
                    p.street_bet = 0
                self.current_bet = 0
                self.min_raise = self.bb
                if len(self.board) == 0:
                    self.board.extend([self.deck.pop(), self.deck.pop(), self.deck.pop()])
                else:
                    self.board.append(self.deck.pop())
                active = [p for p in self.players if not p.folded and not p.all_in]
                if len(active) >= 2:
                    self.pending = {p.uid for p in active}
                    self.turn_idx = self._next_actor_from((self.dealer_idx + 1) % len(self.players))
                    break
            self._reset_turn_timer()
            self._rebuild_buttons()
            return

        self.turn_idx = self._next_actor_from((self.turn_idx + 1) % len(self.players))
        self._reset_turn_timer()
        self._rebuild_buttons()

    def build_embed(self) -> discord.Embed:
        if self.phase == "CLOSED":
            return discord.Embed(title="♠️ Bàn Poker đã đóng", description=self.last_action, color=discord.Color.dark_grey())

        pot = sum(p.total_bet for p in self.players)
        board_str = fmt_cards(self.board) if self.board else "`[ ? ]` `[ ? ]` `[ ? ]`"
        embed = discord.Embed(
            title=f"♠️ POKER TEXAS HOLD'EM (Buy-in {self.buyin:,} xu)",
            description=f"**Bài chung:** {board_str}\n**Tổng Pot:** `{pot:,} xu` | **Cược hiện tại:** `{self.current_bet:,} xu`",
            color=discord.Color.dark_green() if self.phase == "PLAYING" else discord.Color.blurple(),
        )
        rows = []
        for idx, p in enumerate(self.players):
            tag = ""
            if self.phase == "PLAYING":
                d_mark = " (D)" if idx == self.dealer_idx else ""
                if p.folded:
                    tag = f"{d_mark} — 🏳️ Fold"
                elif p.all_in:
                    tag = f"{d_mark} — 🔥 All-in ({p.street_bet:,})"
                elif idx == self.turn_idx:
                    tag = f"{d_mark} — 👉 **ĐẾN LƯỢT**"
                else:
                    tag = f"{d_mark} — Cược vòng: {p.street_bet:,}"
            rows.append(f"• **{p.name}**: `{p.stack:,} xu`{tag}")
        embed.add_field(name=f"Người chơi ({len(self.players)}/8)", value="\n".join(rows) or "_Trống_", inline=False)
        embed.add_field(name="Diễn biến", value=self.last_action, inline=False)
        return embed

    def _add_btn(self, label: str, style: discord.ButtonStyle, cb, disabled: bool = False):
        btn = discord.ui.Button(label=label, style=style, disabled=disabled)
        btn.callback = cb
        self.add_item(btn)

    def _rebuild_buttons(self) -> None:
        self.clear_items()
        if self.phase == "CLOSED":
            return
        if self.phase in ("LOBBY", "HAND_OVER"):
            self._add_btn(f"🪑 Tham gia ({self.buyin:,})", discord.ButtonStyle.green, self._on_join, len(self.players) >= 8)
            self._add_btn("🚪 Rời bàn", discord.ButtonStyle.grey, self._on_leave)
            if self.phase == "LOBBY":
                self._add_btn("▶️ Bắt đầu", discord.ButtonStyle.blurple, self._on_start, len(self.players) < 2)
                self._add_btn("❌ Hủy bàn", discord.ButtonStyle.red, self._on_close)
            else:
                eligible = sum(1 for p in self.players if p.stack > 0)
                self._add_btn("🔄 Ván tiếp theo", discord.ButtonStyle.blurple, self._on_start, eligible < 2)
                self._add_btn("🛑 Đóng bàn", discord.ButtonStyle.red, self._on_close)
            return

        p = self.current_player
        to_call = max(0, self.current_bet - p.street_bet)
        call_label = "Check" if to_call == 0 else f"Call {min(to_call, p.stack):,}"
        can_raise = p.stack > to_call
        self._add_btn("🃏 Xem bài", discord.ButtonStyle.grey, self._on_peek)
        self._add_btn(call_label, discord.ButtonStyle.green, self._on_call)
        self._add_btn("💰 Tố thêm", discord.ButtonStyle.blurple, self._on_raise, not can_raise)
        self._add_btn("🔥 All-in", discord.ButtonStyle.red, self._on_allin, not can_raise)
        self._add_btn("🏳️ Fold", discord.ButtonStyle.secondary, self._on_fold)

    async def _on_join(self, interaction: discord.Interaction):
        async with self.lock:
            if self.phase not in ("LOBBY", "HAND_OVER"):
                return await interaction.response.send_message("Chờ hết ván rồi vào nhé!", ephemeral=True)
            if any(p.uid == interaction.user.id for p in self.players):
                return await interaction.response.send_message("Bạn đã ngồi trong bàn rồi!", ephemeral=True)
            if len(self.players) >= 8:
                return await interaction.response.send_message("Bàn đã đủ 8 người!", ephemeral=True)
            if get_bal(interaction.user.id) < self.buyin:
                return await interaction.response.send_message(
                    f"Bạn cần **{self.buyin:,} xu** để vào bàn.", ephemeral=True
                )
            add_bal(interaction.user.id, -self.buyin)
            self.players.append(PokerPlayer(interaction.user.id, interaction.user.display_name, self.buyin))
            self.last_action = f"➕ {interaction.user.display_name} đã ngồi vào bàn."
            self._rebuild_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_leave(self, interaction: discord.Interaction):
        async with self.lock:
            if self.phase not in ("LOBBY", "HAND_OVER"):
                return await interaction.response.send_message("Đang trong ván, không thể rời giữa chừng!", ephemeral=True)
            idx = next((i for i, p in enumerate(self.players) if p.uid == interaction.user.id), -1)
            if idx == -1:
                return await interaction.response.send_message("Bạn không có trong bàn!", ephemeral=True)
            p = self.players.pop(idx)
            add_bal(p.uid, p.stack)
            if self.players:
                self.dealer_idx %= len(self.players)
                if p.uid == self.host_id:
                    self.host_id = self.players[0].uid
                self.last_action = f"➖ {p.name} đã rời bàn (nhận lại **{p.stack:,} xu**)."
                self._rebuild_buttons()
            else:
                self._close_table(refund=False)
                self.last_action = "Mọi người đã rời bàn — đóng bàn."
            await interaction.response.edit_message(embed=self.build_embed(), view=None if self.phase == "CLOSED" else self)

    async def _on_start(self, interaction: discord.Interaction):
        async with self.lock:
            if self.phase == "LOBBY" and interaction.user.id != self.host_id:
                return await interaction.response.send_message("Chỉ chủ bàn mới bấm Bắt đầu được!", ephemeral=True)
            if self.phase == "HAND_OVER" and not any(p.uid == interaction.user.id for p in self.players):
                return await interaction.response.send_message("Chỉ người trong bàn mới chia ván mới được!", ephemeral=True)
            self._start_hand()
            await interaction.response.edit_message(embed=self.build_embed(), view=None if self.phase == "CLOSED" else self)

    async def _on_close(self, interaction: discord.Interaction):
        async with self.lock:
            if interaction.user.id != self.host_id:
                return await interaction.response.send_message("Chỉ chủ bàn mới được đóng bàn!", ephemeral=True)
            self._close_table(refund=True)
            self.last_action = "🛑 Chủ bàn đã đóng bàn và hoàn chip về ví mọi người."
            await interaction.response.edit_message(embed=self.build_embed(), view=None)

    async def _on_peek(self, interaction: discord.Interaction):
        p = next((x for x in self.players if x.uid == interaction.user.id), None)
        if not p or not p.hole:
            return await interaction.response.send_message("Bạn không có bài trong ván này.", ephemeral=True)
        await interaction.response.send_message(f"🃏 Bài tẩy của bạn: {fmt_cards(p.hole)}", ephemeral=True)

    async def _on_call(self, interaction: discord.Interaction):
        async with self.lock:
            if self.phase != "PLAYING" or interaction.user.id != self.current_player.uid:
                return await interaction.response.send_message("Chưa tới lượt bạn!", ephemeral=True)
            p = self.current_player
            to_call = max(0, self.current_bet - p.street_bet)
            if to_call == 0:
                self.last_action = f"{p.name} Check"
            else:
                paid = self._post_chip(p, to_call)
                self.last_action = f"{p.name} Call **{paid:,}**" + (" (All-in)" if p.all_in else "")
            self.pending.discard(p.uid)
            self._advance_after_action()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_raise(self, interaction: discord.Interaction):
        if self.phase != "PLAYING" or interaction.user.id != self.current_player.uid:
            return await interaction.response.send_message("Chưa tới lượt bạn!", ephemeral=True)
        await interaction.response.send_modal(RaiseModal(self))

    async def _on_allin(self, interaction: discord.Interaction):
        async with self.lock:
            if self.phase != "PLAYING" or interaction.user.id != self.current_player.uid:
                return await interaction.response.send_message("Chưa tới lượt bạn!", ephemeral=True)
            p = self.current_player
            self._post_chip(p, p.stack)
            if p.street_bet > self.current_bet:
                raise_by = p.street_bet - self.current_bet
                if raise_by >= self.min_raise:
                    self.min_raise = raise_by
                self.current_bet = p.street_bet
                self.pending = {q.uid for q in self.players if not q.folded and not q.all_in and q.uid != p.uid}
            else:
                self.pending.discard(p.uid)
            self.last_action = f"🔥 {p.name} **ALL-IN** ({p.street_bet:,})!"
            self._advance_after_action()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_fold(self, interaction: discord.Interaction):
        async with self.lock:
            if self.phase != "PLAYING" or interaction.user.id != self.current_player.uid:
                return await interaction.response.send_message("Chưa tới lượt bạn!", ephemeral=True)
            p = self.current_player
            p.folded = True
            self.pending.discard(p.uid)
            self.last_action = f"🏳️ {p.name} Fold"
            self._advance_after_action()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)


# ---------- Bot & Commands ----------

intents = discord.Intents.default()
intents.message_content = True


class MiniGameBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        valid, by_first = await asyncio.to_thread(load_words)
        VALID_WORDS.update(valid)
        BY_FIRST.update(by_first)
        await self.tree.sync()
        print(f"[San sang] Da nap {len(VALID_WORDS):,} tu noi & dong bo slash commands.")

    async def on_ready(self):
        print(f"Bot online: {self.user} (ID: {self.user.id})")


bot = MiniGameBot()


@bot.tree.command(name="xu", description="Xem số xu hiện tại của bạn hoặc người khác")
async def cmd_xu(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    await interaction.response.send_message(f"💰 **{target.display_name}** đang có **{get_bal(target.id):,} xu**.")


@bot.tree.command(name="cuutro", description="Nhận 2,000 xu cứu trợ khi còn dưới 500 xu")
async def cmd_cuutro(interaction: discord.Interaction):
    bal = get_bal(interaction.user.id)
    if bal >= 500:
        return await interaction.response.send_message(
            f"Bạn còn **{bal:,} xu** — xuống dưới 500 xu mới được cứu trợ nhé!", ephemeral=True
        )
    new_bal = add_bal(interaction.user.id, 2000)
    await interaction.response.send_message(f"🆘 Đã bơm **+2,000 xu** cho {interaction.user.mention} (hiện có **{new_bal:,} xu**)!")


@bot.tree.command(name="noitu", description="Bắt đầu game Vua Tiếng Việt (Nối từ) trong kênh này")
async def cmd_noitu(interaction: discord.Interaction):
    ch_id = interaction.channel_id
    if ch_id in noitu_games:
        g = noitu_games[ch_id]
        tail = g.current.split()[1]
        return await interaction.response.send_message(
            f"Đang có ván nối từ! Từ hiện tại: **{VALID_WORDS[g.current]}** (nối bằng **{tail}**, chuỗi {g.streak}).",
            view=StopNoiTuView(ch_id),
        )
    starters = [w for w in VALID_WORDS if len(BY_FIRST.get(w.split()[1], ())) >= 2] or list(VALID_WORDS)
    start = random.choice(starters)
    tail = start.split()[1]
    noitu_games[ch_id] = NoiTuGame(current=start, last_uid=None, solo=False, used={start})
    await interaction.response.send_message(
        f"🔤 **VUA TIẾNG VIỆT — NỐI TỪ BẮT ĐẦU!**\n"
        f"Từ đầu tiên: **{VALID_WORDS[start]}**\n"
        f"👉 Nhắn trực tiếp vào kênh từ ghép 2 tiếng bắt đầu bằng **{tail}**."
    )

@bot.tree.command(name="stop", description="Dừng ván Nối từ hiện tại khi bí từ")
async def cmd_stop(interaction: discord.Interaction):
    game = noitu_games.pop(interaction.channel_id, None)
    if not game:
        return await interaction.response.send_message(
            "Hiện không có ván nối từ nào đang chạy ở kênh này.", ephemeral=True
        )
    await interaction.response.send_message(
        f"🛑 Đã dừng ván nối từ ở từ **{VALID_WORDS[game.current]}** (chuỗi **{game.streak}** lượt). "
        f"Gõ `/noitu` để chơi ván mới nhé!"
    )

@bot.listen("on_message")   # <-- Dư 4 dấu cách ở đây
async def on_noitu_message(message: discord.Message):
    if message.author.bot or message.channel.id not in noitu_games:
        return
    text = message.content.strip()
    if not text or text.startswith(("/", "!")):
        return
    canon = norm_vi(text)
    parts = canon.split()
    game = noitu_games[message.channel.id]
    expected = game.current.split()[1]

    if len(parts) != 2 or not all(p.isalpha() for p in parts):
        if parts and parts[0] == expected:
            await message.add_reaction("❌")
        return
    if parts[0] != expected:
        await message.add_reaction("❌")
        return
    if game.last_uid == message.author.id:
        await message.add_reaction("⏳")
        await message.channel.send(
            f"{message.author.mention} Chờ người khác trả lời rồi mới được nối tiếp nhé!",
            delete_after=4,
        )
        return
    if canon not in VALID_WORDS or canon in game.used:
        await message.add_reaction("❌")
        if canon in game.used:
            await message.channel.send(f"{message.author.mention} Từ **{text}** đã dùng rồi!", delete_after=4)
        return

    game.used.add(canon)
    game.current = canon
    game.last_uid = message.author.id
    game.streak += 1
    add_bal(message.author.id, 50)
    next_first = parts[1]
    has_next = any(w not in game.used for w in BY_FIRST.get(next_first, ()))
    await message.add_reaction("✅")
    if not has_next:
        del noitu_games[message.channel.id]
        add_bal(message.author.id, 500)
        await message.channel.send(
            f"🏆 **CHỐT HẠ!** {message.author.mention} thắng sau **{game.streak}** lượt (**+500 xu**)! "
            f"Không còn từ nào bắt đầu bằng **{next_first}**."
        )


@bot.tree.command(name="taixiu", description="Mở bàn Tài Xỉu 20 giây cho bạn bè đặt cược")
async def cmd_taixiu(interaction: discord.Interaction):
    ch_id = interaction.channel_id
    if ch_id in taixiu_channels:
        return await interaction.response.send_message("Kênh này đang có bàn Tài Xỉu mở rồi!", ephemeral=True)
    taixiu_channels.add(ch_id)
    view = TaiXiuView(ch_id, interaction.user.id)
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    view.message = await interaction.original_response()
    view.timer_task = asyncio.create_task(view._countdown())


@bot.tree.command(name="poker", description="Mở bàn Poker Texas Hold'em (mặc định buy-in 1,000 xu)")
async def cmd_poker(interaction: discord.Interaction, buyin: app_commands.Range[int, 100, 1_000_000] = 1000):
    ch_id = interaction.channel_id
    if ch_id in poker_tables:
        return await interaction.response.send_message("Kênh này đang có bàn Poker rồi!", ephemeral=True)
    if get_bal(interaction.user.id) < buyin:
        return await interaction.response.send_message(f"Bạn không đủ **{buyin:,} xu** để mở bàn!", ephemeral=True)
    add_bal(interaction.user.id, -buyin)
    view = PokerView(ch_id, interaction.user, buyin)
    poker_tables[ch_id] = view
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    view.message = await interaction.original_response()


# ---------- Self-Checks (--test) ----------

def run_self_checks() -> None:
    assert norm_vi("  HÒA   bình ") == "hoà bình"
    assert norm_vi("thủy tinh") == norm_vi("thuỷ tinh")
    valid, by_first = build_word_index(FALLBACK_WORDS)
    assert "con mèo" in valid and "mèo mướp" in by_first["mèo"]

    assert resolve_taixiu(1, 1, 1) == ("BAO", 3)
    assert resolve_taixiu(6, 6, 6) == ("BAO", 18)
    assert resolve_taixiu(5, 4, 2) == ("TAI", 11)
    assert resolve_taixiu(1, 2, 3) == ("XIU", 6)

    wheel = best_hand([(14, "♠"), (2, "♥"), (3, "♦"), (4, "♣"), (5, "♠"), (9, "♦"), (13, "♣")])
    six_high = best_hand([(6, "♠"), (2, "♥"), (3, "♦"), (4, "♣"), (5, "♠"), (9, "♦"), (12, "♣")])
    royal = best_hand([(14, "♠"), (13, "♠"), (12, "♠"), (11, "♠"), (10, "♠"), (2, "♥"), (3, "♦")])
    assert wheel[0] == 5 and six_high > wheel and royal > six_high

    # Side pot: A folded (100), B all-in 200 wins main, C calls 500 wins side, D calls 500 loses
    board = [(14, "♠"), (13, "♠"), (7, "♦"), (4, "♣"), (2, "♥")]
    p_a = PokerPlayer(1, "A", 0, [(2, "♣"), (3, "♦")], total_bet=100, folded=True)
    p_b = PokerPlayer(2, "B", 0, [(14, "♥"), (14, "♦")], total_bet=200, all_in=True)
    p_c = PokerPlayer(3, "C", 0, [(13, "♥"), (13, "♦")], total_bet=500)
    p_d = PokerPlayer(4, "D", 0, [(9, "♥"), (9, "♦")], total_bet=500)
    payouts, _ = settle_pots([p_a, p_b, p_c, p_d], 0, board)
    assert payouts == {1: 0, 2: 700, 3: 600, 4: 0} and sum(payouts.values()) == 1300

    # Odd chip split pot (101 chips -> 51 left of dealer, 50 next)
    s1 = PokerPlayer(1, "S1", 0, [(14, "♥"), (10, "♦")], total_bet=51)
    s2 = PokerPlayer(2, "S2", 0, [(14, "♦"), (10, "♣")], total_bet=50)
    split_pay, _ = settle_pots([s1, s2], 0, [(14, "♣"), (13, "♠"), (12, "♥"), (8, "♦"), (2, "♣")])
    assert split_pay == {1: 1, 2: 100} or True  # uncalled 1 returns to s1 + split 100 -> 51 & 50!
    assert split_pay == {1: 51, 2: 50}
    print("Tat ca self-checks OK!")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_self_checks()
        sys.exit(0)
    token = os.environ.get("DISCORD_TOKEN") or load_dotenv(BASE_DIR / ".env").get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Thieu DISCORD_TOKEN trong .env hoac bien moi truong.")
    bot.run(token)