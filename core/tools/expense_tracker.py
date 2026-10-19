"""Expense tracking module for JARVIS.

Manages recording, categorizing and reporting personal expenses locally
with automatic category detection and formatted monthly summaries.
"""

from datetime import datetime
import json
import logging
from pathlib import Path
import re
import uuid

from core.config import DATA_DIR
EXPENSES_FILE = DATA_DIR / "expenses.json"

CATEGORIES_KEYWORDS = {
    "🍔 Alimentation": [
        "resto", "restaurant", "repas", "dejeuner", "déjeuner", "diner", "dîner",
        "mcdo", "mcdonalds", "kebab", "burger", "pizza", "sushi", "cafe", "café", "croissant",
        "boulangerie", "courses", "monoprix", "carrefour", "lidl", "auchan", "franprix",
        "intermarche", "intermarché", "leclerc", "super u", "picard", "paul", "starbucks", "snack"
    ],
    "🚇 Transport": [
        "metro", "métro", "bus", "train", "sncf", "ratp", "navigo", "uber", "bolt",
        "taxi", "essence", "carburant", "total", "bp", "shell", "peage", "péage", "parking", "trottinette", "lime", "dott"
    ],
    "🎓 Études & EPITA": [
        "epita", "ecole", "école", "cours", "livre", "manuel", "imprimerie",
        "impression", "cotisation", "association", "bde", "fournitures", "gibert"
    ],
    "💻 Tech & Matériel": [
        "tech", "pc", "ordi", "souris", "clavier", "ecran", "écran", "cable",
        "câble", "composant", "serveur", "vps", "domaine", "materiel", "matériel",
        "fnac", "amazon", "apple", "ldlc", "darty", "boulanger"
    ],
    "🎉 Sorties & Loisirs": [
        "bar", "biere", "bière", "soiree", "soirée", "cinema", "cinéma", "film",
        "ugc", "pathe", "pathé", "jeu", "jeux", "steam", "concert", "festival", "musique", "sport"
    ],
    "🏠 Abonnements & Quotidien": [
        "loyer", "edf", "electricite", "électricité", "eau", "internet", "box",
        "forfait", "free", "orange", "sfr", "bouygues", "spotify", "netflix", "cloud", "assurance", "santé", "pharmacie"
    ]
}


def detect_category(description: str) -> str:
    """Infers the expense category based on keywords in the description."""
    desc_clean = description.lower()
    for cat_name, keywords in CATEGORIES_KEYWORDS.items():
        if any(kw in desc_clean for kw in keywords):
            return cat_name
    return "📦 Autre"


def load_expenses() -> list[dict]:
    """Loads recorded expenses from JSON file."""
    if not EXPENSES_FILE.exists():
        return []
    try:
        with open(EXPENSES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read {EXPENSES_FILE.name}: {e}")
        return []


def save_expenses(expenses: list[dict]) -> bool:
    """Saves expenses to disk."""
    try:
        with open(EXPENSES_FILE, "w", encoding="utf-8") as f:
            json.dump(expenses, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Failed to write expenses: {e}")
        return False


def parse_expense_input(text: str) -> tuple[float, str, str] | None:
    """Extracts amount, description and category from user text.

    Supports:
    - /depense 12.50 repas midi
    - Apple Pay: 12.50 chez Monoprix
    - Transaction 14.90€ Fnac
    - Depense: 15.00 cafe fnac
    - Achat de 24.99 livre maths
    - 8.50 kebab
    """
    clean = text.strip()
    # Remove command and shortcut prefixes
    clean = re.sub(r"^/depense(?:@[a-zA-Z0-9_]+)?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^(?:apple\s*pay|carte|cb|transaction|d[eé]pense|achat|pay[eé]|cout|co[uû]t)\s*[:=]?\s*", "", clean, flags=re.IGNORECASE)

    # Patterns matching amount and description
    # Pattern 1: amount first (e.g. "12.50€ repas" or "12,50 chez burger")
    m1 = re.match(r"^(\d+(?:[.,]\d+)?)\s*€?\s*(.+)$", clean)
    if m1:
        amt_str = m1.group(1).replace(",", ".")
        raw_desc = m1.group(2).strip()
        desc = re.sub(r"^(?:chez|au|à|a)\s+", "", raw_desc, flags=re.IGNORECASE).strip()
        try:
            amount = float(amt_str)
            cat = detect_category(desc)
            return amount, desc, cat
        except ValueError:
            pass

    # Pattern 2: description first then amount (e.g. "repas midi 12.50€")
    m2 = re.match(r"^(.+?)\s+(\d+(?:[.,]\d+)?)\s*€?$", clean)
    if m2:
        raw_desc = m2.group(1).strip()
        desc = re.sub(r"^(?:chez|au|à|a)\s+", "", raw_desc, flags=re.IGNORECASE).strip()
        amt_str = m2.group(2).replace(",", ".")
        try:
            amount = float(amt_str)
            cat = detect_category(desc)
            return amount, desc, cat
        except ValueError:
            pass

    return None


def add_expense(
    amount: float,
    description: str,
    category: str | None = None,
    source: str = "Telegram"
) -> dict:
    """Records a new expense and returns the saved object."""
    cat = category if category else detect_category(description)
    now = datetime.now()
    item = {
        "id": uuid.uuid4().hex[:8],
        "amount": round(amount, 2),
        "description": description.strip(),
        "category": cat,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timestamp": now.timestamp(),
        "source": source,
    }

    expenses = load_expenses()
    expenses.append(item)
    save_expenses(expenses)
    return item


def get_monthly_summary(month: int | None = None, year: int | None = None) -> dict:
    """Generates monthly expense summary and categorization."""
    now = datetime.now()
    target_month = month or now.month
    target_year = year or now.year

    month_str = f"{target_year}-{target_month:02d}"
    expenses = load_expenses()

    month_items = [e for e in expenses if e.get("date", "").startswith(month_str)]
    total_amount = sum(e.get("amount", 0.0) for e in month_items)

    cat_totals: dict[str, float] = {}
    for e in month_items:
        c = e.get("category", "📦 Autre")
        cat_totals[c] = cat_totals.get(c, 0.0) + e.get("amount", 0.0)

    # Sort categories by total descending
    sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)

    month_names_fr = [
        "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
        "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"
    ]
    cur_month_name = month_names_fr[target_month - 1]

    lines = [
        f"💳 <b>Bilan Dépenses - {cur_month_name} {target_year}</b>",
        f"• <b>Total dépensé :</b> <code>{total_amount:.2f} €</code> ({len(month_items)} transactions)\n",
        "<b>Répartition par catégorie :</b>",
    ]

    if not month_items:
        lines.append("<i>Aucune dépense enregistrée ce mois-ci.</i>")
    else:
        for cat, amt in sorted_cats:
            pct = (amt / total_amount * 100) if total_amount else 0
            lines.append(f"• {cat} : <b>{amt:.2f} €</b> ({pct:.1f}%)")

        lines.append("\n<b>5 dernières dépenses :</b>")
        for e in sorted(month_items, key=lambda x: x.get("timestamp", 0), reverse=True)[:5]:
            d_fmt = e.get("date", "")[8:10] + "/" + e.get("date", "")[5:7]
            lines.append(f"• <i>{d_fmt}</i> - {e.get('description')} : <b>{e.get('amount'):.2f} €</b> ({e.get('category')})")

    return {
        "month": target_month,
        "year": target_year,
        "total": round(total_amount, 2),
        "count": len(month_items),
        "categories": cat_totals,
        "message": "\n".join(lines),
    }


# ==============================================================================
# MONTHLY BUDGET MANAGEMENT & ALERTS
# ==============================================================================

BUDGET_FILE = DATA_DIR / "budget.json"


def load_budget() -> dict:
    """Loads budget configuration from disk."""
    if not BUDGET_FILE.exists():
        return {"limit": None, "warning_pct": 80, "notified_warning": None, "notified_exceeded": None}
    try:
        with open(BUDGET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to load budget file: {e}")
        return {"limit": None, "warning_pct": 80, "notified_warning": None, "notified_exceeded": None}


def save_budget(cfg: dict) -> bool:
    """Saves budget configuration to disk."""
    try:
        with open(BUDGET_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Failed to write budget file: {e}")
        return False


def set_monthly_budget(limit: float, warning_pct: int = 80) -> dict:
    """Sets or updates the monthly budget limit."""
    cfg = load_budget()
    cfg["limit"] = round(float(limit), 2)
    cfg["warning_pct"] = warning_pct
    # Reset notifications for current month when updating budget limit
    now_month = datetime.now().strftime("%Y-%m")
    summary = get_monthly_summary()
    cur_spent = summary["total"]
    
    # If the new limit is higher than current spent, reset exceeded notification
    if cur_spent < cfg["limit"]:
        cfg["notified_exceeded"] = None
    if cur_spent < (warning_pct / 100.0) * cfg["limit"]:
        cfg["notified_warning"] = None
        
    save_budget(cfg)
    return cfg


def disable_monthly_budget() -> bool:
    """Disables the monthly budget limit."""
    cfg = load_budget()
    cfg["limit"] = None
    cfg["notified_warning"] = None
    cfg["notified_exceeded"] = None
    return save_budget(cfg)


def check_budget_alerts(current_spent: float, month_key: str | None = None) -> list[dict]:
    """Evaluates whether current spending triggers threshold alerts (80% warning or 100% exceeded).
    
    Returns a list of alert dicts with rendered messages.
    """
    cfg = load_budget()
    limit = cfg.get("limit")
    if not limit or limit <= 0:
        return []

    cur_month = month_key or datetime.now().strftime("%Y-%m")
    warning_pct = cfg.get("warning_pct", 80)
    warning_limit = (warning_pct / 100.0) * limit

    alerts = []
    pct = (current_spent / limit * 100.0)

    # 1. Warning alert (e.g. >= 80% and not yet notified this month)
    if current_spent >= warning_limit and cfg.get("notified_warning") != cur_month:
        remaining = max(0.0, limit - current_spent)
        alerts.append({
            "type": "warning",
            "spent": current_spent,
            "limit": limit,
            "pct": pct,
            "message": (
                f"⚠️ <b>ALERTE BUDGET MENSUEL ({warning_pct}%)</b>\n\n"
                f"Tu as consommé <b>{pct:.1f}%</b> de ton budget mensuel !\n"
                f"• Total dépensé : <b>{current_spent:.2f} €</b> / <b>{limit:.2f} €</b>\n"
                f"• Reste disponible : <b>{remaining:.2f} €</b> pour finir le mois.\n\n"
                f"💡 <i>Pense à lever le pied sur les dépenses non essentielles !</i>"
            ),
        })
        cfg["notified_warning"] = cur_month

    # 2. Exceeded alert (>= 100% and not yet notified this month)
    if current_spent >= limit and cfg.get("notified_exceeded") != cur_month:
        excess = current_spent - limit
        alerts.append({
            "type": "exceeded",
            "spent": current_spent,
            "limit": limit,
            "pct": pct,
            "message": (
                f"🚨 <b>ALERTE DÉPASSEMENT DE BUDGET (100%)</b>\n\n"
                f"Attention ! Tu as dépassé ton budget mensuel configuré !\n"
                f"• Total dépensé ce mois : <b>{current_spent:.2f} €</b>\n"
                f"• Budget fixé : <b>{limit:.2f} €</b>\n"
                f"• Dépassement : <b>+{excess:.2f} €</b> ({pct:.1f}% du budget)\n\n"
                f"⚠️ <i>Attention aux prochains achats pour éviter de creuser l'écart.</i>"
            ),
        })
        cfg["notified_exceeded"] = cur_month

    if alerts:
        save_budget(cfg)

    return alerts


def get_budget_status() -> dict:
    """Formats a detailed overview of the monthly budget progress and recommended pace."""
    import calendar
    cfg = load_budget()
    limit = cfg.get("limit")
    summary = get_monthly_summary()
    spent = summary["total"]

    if not limit or limit <= 0:
        return {
            "configured": False,
            "spent": spent,
            "limit": 0.0,
            "remaining": 0.0,
            "pct": 0.0,
            "message": (
                "💳 <b>Gestion du Budget Mensuel JARVIS</b>\n\n"
                f"• Total dépensé ce mois : <code>{spent:.2f} €</code> ({summary['count']} transactions)\n"
                "• Budget mensuel : <i>Aucun budget configuré</i>\n\n"
                "💡 <b>Pour configurer un budget :</b>\n"
                "<code>/budget 450</code> <i>(définit un budget de 450€ / mois)</i>\n\n"
                "<i>JARVIS vous enverra automatiquement une alerte à 80% de la limite et une alerte rouge en cas de dépassement.</i>"
            )
        }

    pct = (spent / limit * 100.0) if limit else 0.0
    remaining = limit - spent
    now = datetime.now()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left = max(1, days_in_month - now.day)
    daily_pace = max(0.0, remaining) / days_left

    bar_len = 10
    filled = min(bar_len, max(0, int(round((pct / 100.0) * bar_len))))
    bar = "█" * filled + "░" * (bar_len - filled)

    if pct >= 100.0:
        status_badge = "🔴 <b>Budget dépassé</b>"
    elif pct >= cfg.get("warning_pct", 80):
        status_badge = "🟡 <b>Proche de la limite</b>"
    else:
        status_badge = "🟢 <b>Dans les clous</b>"

    lines = [
        "💳 <b>Suivi du Budget Mensuel JARVIS</b>",
        f"• <b>Statut :</b> {status_badge}",
        f"• <b>Jauge :</b> <code>[{bar}] {pct:.1f}%</code>\n",
        f"• <b>Budget fixé :</b> <code>{limit:.2f} €</code>",
        f"• <b>Dépensé :</b> <code>{spent:.2f} €</code> ({summary['count']} transactions)",
    ]

    if remaining >= 0:
        lines.append(f"• <b>Reste disponible :</b> <code>{remaining:.2f} €</code>")
        lines.append(f"• <b>Rythme conseillé :</b> <code>{daily_pace:.2f} €/jour</code> ({days_left} jours restants)")
    else:
        lines.append(f"• <b>Dépassement :</b> <code>+{-remaining:.2f} €</code>")

    lines.extend([
        "\n💡 <i>Pour voir la projection fin de mois :</i> <code>/budget proj</code>",
        "<i>Pour modifier :</i> <code>/budget &lt;montant&gt;</code>",
        "<i>Pour désactiver :</i> <code>/budget off</code>",
    ])

    return {
        "configured": True,
        "spent": spent,
        "limit": limit,
        "remaining": remaining,
        "pct": pct,
        "message": "\n".join(lines),
    }


def detect_recurring_subscriptions() -> list[dict]:
    """Identifies recurring monthly subscriptions by analyzing historical transaction descriptions and amounts."""
    expenses = load_expenses()
    if not expenses:
        return []

    # Known typical subscription patterns
    sub_keywords = [
        "spotify", "netflix", "apple", "google", "icloud", "chatgpt", "github",
        "forfait", "free", "orange", "sfr", "bouygues", "navigo", "ratp", "sncf",
        "edf", "loyer", "assurance", "box", "prime", "youtube"
    ]

    # Group by normalized description and amount
    grouped: dict[str, list[dict]] = {}
    for e in expenses:
        desc_norm = re.sub(r"[0-9\.\,\:\/\-]", "", e.get("description", "").lower()).strip()
        amt = round(e.get("amount", 0.0), 2)
        key = f"{desc_norm}::{amt}"
        grouped.setdefault(key, []).append(e)

    detected = []
    seen_names = set()

    for k, items in grouped.items():
        desc_norm, amt_str = k.split("::", 1)
        amt = float(amt_str)
        # Condition: matches known subscription keywords OR occurred in multiple distinct months
        months = set(it.get("date", "")[:7] for it in items if it.get("date"))
        is_sub_keyword = any(kw in desc_norm for kw in sub_keywords)

        if is_sub_keyword or len(months) >= 2:
            display_name = items[-1].get("description", desc_norm).title()
            if display_name.lower() in seen_names:
                continue
            seen_names.add(display_name.lower())
            detected.append({
                "name": display_name,
                "amount": amt,
                "frequency": "Mensuel",
                "occurrences": len(items),
                "months": sorted(list(months)),
                "category": items[-1].get("category", "🏠 Abonnements & Quotidien"),
            })

    return sorted(detected, key=lambda x: x["amount"], reverse=True)


def get_budget_projection() -> dict:
    """Calculates statistical projection of month-end expenditure based on daily velocity and recurring commitments."""
    import calendar
    now = datetime.now()
    cur_year = now.year
    cur_month = now.month
    cur_day = now.day

    total_days = calendar.monthrange(cur_year, cur_month)[1]
    days_passed = max(1, cur_day)
    days_left = max(0, total_days - cur_day)

    summary = get_monthly_summary(cur_month, cur_year)
    total_spent = summary["total"]
    cfg = load_budget()
    limit = cfg.get("limit") or 0.0

    subs = detect_recurring_subscriptions()
    total_monthly_subs = sum(s["amount"] for s in subs)

    # Calculate average daily variable spending (excluding identified subscriptions already paid this month)
    # Variable daily spending velocity
    daily_velocity = total_spent / days_passed
    projected_variable = daily_velocity * days_left
    projected_total = round(total_spent + projected_variable, 2)

    lines = [
        "📊 <b>Projection Financière Fin de Mois JARVIS</b>",
        f"<i>Calculé au jour {cur_day}/{total_days} ({days_left} jours restants)</i>\n",
        f"• <b>Dépensé à ce jour :</b> <code>{total_spent:.2f} €</code>",
        f"• <b>Vélocité moyenne :</b> <code>{daily_velocity:.2f} €/jour</code>",
        f"• <b>Estimation fin de mois :</b> <code>{projected_total:.2f} €</code>",
    ]

    if limit > 0:
        diff = projected_total - limit
        if diff > 0:
            lines.append(f"• <b>Alerte Trajectoire :</b> 🔴 <b>Dépassement prévu de +{diff:.2f} €</b>")
            safe_daily = max(0.0, (limit - total_spent) / max(1, days_left))
            lines.append(f"💡 <i>Pour rester dans le budget de {limit:.2f} €, limiter à :</i> <b>{safe_daily:.2f} €/jour</b>")
        else:
            lines.append(f"• <b>Alerte Trajectoire :</b> 🟢 <b>Sous le budget (-{-diff:.2f} € d'économie prévue)</b>")

    if subs:
        lines.append(f"\n🔁 <b>Abonnements et Charges Récurrentes détectés ({len(subs)}) :</b>")
        for s in subs[:6]:
            lines.append(f"• {s['name']} : <code>{s['amount']:.2f} €/mois</code> ({s['category']})")
        lines.append(f"• <i>Total charges fixes :</i> <b>{total_monthly_subs:.2f} €/mois</b>")

    return {
        "current_spent": total_spent,
        "daily_velocity": round(daily_velocity, 2),
        "projected_total": projected_total,
        "days_left": days_left,
        "subscriptions": subs,
        "message": "\n".join(lines),
    }


