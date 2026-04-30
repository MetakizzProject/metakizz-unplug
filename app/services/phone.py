"""Phone number normalization + country detection.

Wraps Google's libphonenumber so we can:
- Normalize raw phone strings to E.164 format (+34612345678)
- Extract the ISO 3166-1 alpha-2 country code (e.g. "ES")
- Map that to a human country name + flag emoji for the admin

Used at signup time (GHL webhook) and during the CSV backfill flow.
Failures (invalid number, unknown region) return None gracefully.
"""

import logging
import phonenumbers

logger = logging.getLogger(__name__)


# ISO alpha-2 → (country name, flag emoji). Curated list covering the
# top ~80 origin countries we expect for the campaign. Anything missing
# falls back to the ISO code itself ("MX" stays "MX").
ISO_TO_COUNTRY = {
    "ES": ("Spain", "🇪🇸"),
    "FR": ("France", "🇫🇷"),
    "DE": ("Germany", "🇩🇪"),
    "IT": ("Italy", "🇮🇹"),
    "PT": ("Portugal", "🇵🇹"),
    "GB": ("United Kingdom", "🇬🇧"),
    "IE": ("Ireland", "🇮🇪"),
    "NL": ("Netherlands", "🇳🇱"),
    "BE": ("Belgium", "🇧🇪"),
    "LU": ("Luxembourg", "🇱🇺"),
    "CH": ("Switzerland", "🇨🇭"),
    "AT": ("Austria", "🇦🇹"),
    "DK": ("Denmark", "🇩🇰"),
    "SE": ("Sweden", "🇸🇪"),
    "NO": ("Norway", "🇳🇴"),
    "FI": ("Finland", "🇫🇮"),
    "IS": ("Iceland", "🇮🇸"),
    "PL": ("Poland", "🇵🇱"),
    "CZ": ("Czechia", "🇨🇿"),
    "SK": ("Slovakia", "🇸🇰"),
    "HU": ("Hungary", "🇭🇺"),
    "RO": ("Romania", "🇷🇴"),
    "BG": ("Bulgaria", "🇧🇬"),
    "HR": ("Croatia", "🇭🇷"),
    "SI": ("Slovenia", "🇸🇮"),
    "RS": ("Serbia", "🇷🇸"),
    "GR": ("Greece", "🇬🇷"),
    "TR": ("Turkey", "🇹🇷"),
    "RU": ("Russia", "🇷🇺"),
    "UA": ("Ukraine", "🇺🇦"),
    "BY": ("Belarus", "🇧🇾"),
    "LT": ("Lithuania", "🇱🇹"),
    "LV": ("Latvia", "🇱🇻"),
    "EE": ("Estonia", "🇪🇪"),
    "MT": ("Malta", "🇲🇹"),
    "CY": ("Cyprus", "🇨🇾"),
    "ME": ("Montenegro", "🇲🇪"),
    "MK": ("North Macedonia", "🇲🇰"),
    "BA": ("Bosnia & Herzegovina", "🇧🇦"),
    "AL": ("Albania", "🇦🇱"),
    "MD": ("Moldova", "🇲🇩"),
    "GE": ("Georgia", "🇬🇪"),
    "AM": ("Armenia", "🇦🇲"),
    "AZ": ("Azerbaijan", "🇦🇿"),
    "KZ": ("Kazakhstan", "🇰🇿"),
    "MN": ("Mongolia", "🇲🇳"),
    "LB": ("Lebanon", "🇱🇧"),
    "QA": ("Qatar", "🇶🇦"),
    "BH": ("Bahrain", "🇧🇭"),
    "MR": ("Mauritania", "🇲🇷"),
    "RE": ("RÊunion", "🇷🇪"),
    "MQ": ("Martinique", "🇲🇶"),
    "GP": ("Guadeloupe", "🇬🇵"),
    "LU": ("Luxembourg", "🇱🇺"),
    "TT": ("Trinidad & Tobago", "🇹🇹"),
    "JM": ("Jamaica", "🇯🇲"),

    "US": ("United States", "🇺🇸"),
    "CA": ("Canada", "🇨🇦"),
    "MX": ("Mexico", "🇲🇽"),

    "BR": ("Brazil", "🇧🇷"),
    "AR": ("Argentina", "🇦🇷"),
    "CL": ("Chile", "🇨🇱"),
    "CO": ("Colombia", "🇨🇴"),
    "PE": ("Peru", "🇵🇪"),
    "VE": ("Venezuela", "🇻🇪"),
    "EC": ("Ecuador", "🇪🇨"),
    "BO": ("Bolivia", "🇧🇴"),
    "PY": ("Paraguay", "🇵🇾"),
    "UY": ("Uruguay", "🇺🇾"),
    "DO": ("Dominican Republic", "🇩🇴"),
    "CU": ("Cuba", "🇨🇺"),
    "PR": ("Puerto Rico", "🇵🇷"),
    "GT": ("Guatemala", "🇬🇹"),
    "HN": ("Honduras", "🇭🇳"),
    "SV": ("El Salvador", "🇸🇻"),
    "NI": ("Nicaragua", "🇳🇮"),
    "CR": ("Costa Rica", "🇨🇷"),
    "PA": ("Panama", "🇵🇦"),

    "MA": ("Morocco", "🇲🇦"),
    "DZ": ("Algeria", "🇩🇿"),
    "TN": ("Tunisia", "🇹🇳"),
    "EG": ("Egypt", "🇪🇬"),
    "ZA": ("South Africa", "🇿🇦"),
    "NG": ("Nigeria", "🇳🇬"),
    "KE": ("Kenya", "🇰🇪"),
    "GH": ("Ghana", "🇬🇭"),
    "SN": ("Senegal", "🇸🇳"),
    "CI": ("Ivory Coast", "🇨🇮"),
    "AO": ("Angola", "🇦🇴"),
    "MZ": ("Mozambique", "🇲🇿"),
    "CV": ("Cape Verde", "🇨🇻"),

    "AE": ("UAE", "🇦🇪"),
    "SA": ("Saudi Arabia", "🇸🇦"),
    "IL": ("Israel", "🇮🇱"),
    "IN": ("India", "🇮🇳"),
    "PK": ("Pakistan", "🇵🇰"),
    "BD": ("Bangladesh", "🇧🇩"),
    "LK": ("Sri Lanka", "🇱🇰"),
    "PH": ("Philippines", "🇵🇭"),
    "ID": ("Indonesia", "🇮🇩"),
    "MY": ("Malaysia", "🇲🇾"),
    "SG": ("Singapore", "🇸🇬"),
    "TH": ("Thailand", "🇹🇭"),
    "VN": ("Vietnam", "🇻🇳"),
    "JP": ("Japan", "🇯🇵"),
    "KR": ("South Korea", "🇰🇷"),
    "CN": ("China", "🇨🇳"),
    "TW": ("Taiwan", "🇹🇼"),
    "HK": ("Hong Kong", "🇭🇰"),

    "AU": ("Australia", "🇦🇺"),
    "NZ": ("New Zealand", "🇳🇿"),
}


def parse(raw_phone, default_region=None):
    """Parse a raw phone string into (e164, country_code, country_name, flag).

    Returns None if the number can't be parsed or is invalid.

    `default_region` is used as fallback when the input lacks a country
    prefix (e.g. "612345678" with default_region="ES" → +34612345678).
    Most of our inputs have explicit "+34..." so default is rarely needed.
    """
    if not raw_phone:
        return None
    raw = str(raw_phone).strip()
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    region = phonenumbers.region_code_for_number(parsed)
    name, flag = ISO_TO_COUNTRY.get(region, (region, ""))
    return {
        "e164": e164,
        "country_code": region,
        "country_name": name,
        "flag": flag,
    }


def lookup_country(iso_code):
    """ISO 3166-1 alpha-2 → (name, flag). Returns (iso_code, '') if unknown."""
    if not iso_code:
        return ("Unknown", "🌐")
    return ISO_TO_COUNTRY.get(iso_code, (iso_code, ""))
