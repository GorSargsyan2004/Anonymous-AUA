"""Service to extract student first names from AUA emails and infer gender."""

from typing import Optional, Set, Tuple
import re

# Comprehensive dictionary of Armenian, Diaspora, and international male first names (lowercase)
# Includes common Armenian Latin transliteration variants (e.g. David/Davit, Haik/Hayk, etc.)
MALE_NAMES: Set[str] = {
    # Traditional & Modern Armenian Male Names
    "abgar", "abraham", "albert", "alen", "alexander", "aleksey", "alexei", "alex",
    "alik", "anania", "anastas", "andranik", "antranig", "antranik", "ara", "aram",
    "aramais", "ararat", "arayik", "areg", "ares", "aris", "aristakes", "arkadi",
    "arkady", "arman", "armen", "armik", "arsen", "arshak", "artak", "artashes",
    "artavazd", "artem", "arthur", "artur", "artush", "arutyun", "arshavir", "ashot",
    "ashtnak", "avet", "avetik", "avetis", "azat", "babken", "bagrat", "barseg",
    "benik", "benjamin", "boghos", "boris", "daniel", "daniyel", "davit", "david",
    "davood", "davo", "dikran", "edgar", "edmond", "eduard", "edward", "elmir",
    "emil", "erik", "eric", "gagik", "gago", "garegin", "garik", "garnik",
    "gaspar", "gegham", "george", "gevorg", "gevork", "gevor", "ghazar", "ghukas",
    "giorgi", "gor", "grigor", "grigori", "grikor", "gurgen", "hachik", "hacob",
    "hakob", "hagop", "hamlet", "harout", "harutyun", "harut", "hartun", "hasan",
    "hayk", "haik", "haykaz", "henrik", "henry", "hovhannes", "hovo", "hovsep",
    "hrach", "hrachya", "hrayr", "hrazdan", "ishkhan", "ishxan", "jivan", "kamo", "karapet",
    "karen", "karo", "khachatur", "xachatur", "khachik", "xachik", "khachig", "xachig",
    "khoren", "xoren", "khosrov", "xosrov", "kirakos", "knyaz", "koryun", "leo",
    "leon", "levik", "levon", "lev", "loris", "lucas", "luka", "manvel",
    "marat", "mark", "martik", "martin", "matevos", "meruzhan", "mher", "mikael",
    "michael", "mikayel", "misha", "mkhitar", "mxitar", "movses", "murad", "mushegh", "narek",
    "nareg", "nelson", "nerses", "norayr", "norik", "paruyr", "pavel", "petros",
    "petur", "peter", "rafayel", "rafael", "rafik", "razmik", "robert", "roman",
    "ruben", "rouben", "rustam", "sahak", "samson", "samvel", "sargis", "sarkis",
    "sarhad", "saro", "sayat", "sedrak", "sergey", "serj", "serzh", "setrak",
    "sevak", "sevan", "shant", "shavarsh", "simon", "smbat", "sos", "spartak",
    "stepan", "stephen", "suren", "taron", "tatevos", "tatul", "tigran", "toros",
    "vahagn", "vahan", "vahram", "vahe", "valerik", "valeri", "vanik", "varazdat",
    "vardan", "vartan", "vazgen", "vigen", "viktor", "victor", "vladimir", "volodya",
    "vrach", "vram", "vrezh", "yeghishe", "yervand", "yuri", "zaven", "zohrab",
    "zorik", "zorayr", "zhora", "adam", "alan", "alexis", "andrew", "anthony",
    "anton", "artyom", "christian", "denis", "dmitri", "dmitry", "felix", "gleb",
    "igor", "ivan", "jack", "james", "john", "joseph", "kirill", "konstantin",
    "maksim", "maxim", "matthew", "nikita", "nikolay", "oleg", "paul", "sam",
    "sergei", "stanislav", "timur", "vladislav", "vlad", "yaroslav", "yuriy",
}

# Comprehensive dictionary of Armenian, Diaspora, and international female first names (lowercase)
FEMALE_NAMES: Set[str] = {
    # Traditional & Modern Armenian Female Names
    "adela", "adeline", "adelina", "aghavni", "ida", "aida", "alice", "alisa",
    "alla", "almast", "alvard", "amalya", "anahit", "anastasia", "aneta", "angela",
    "angelina", "ani", "anichka", "anita", "anna", "ann", "anush", "anushik",
    "araqs", "araqsya", "araks", "araksya", "arev", "arevik", "armin", "armine",
    "armina", "arpenik", "arpen", "arpi", "arpik", "arpine", "arsine", "arshaluys",
    "artemisa", "ashkhen", "ashxen", "aspram", "astgh", "astghik", "astrid", "avrora", "bella",
    "camilla", "daria", "darya", "diana", "dina", "dzovinar", "elena", "elenoire",
    "eleonora", "elen", "elina", "eliza", "elizabeth", "ella", "elmira", "elsa",
    "elvira", "emma", "ester", "eva", "evgenia", "evelina", "flora", "galina",
    "gauhar", "gayane", "gayan", "gohar", "hameshik", "hasmik", "heghine", "helen",
    "helena", "hermine", "hripsime", "hrush", "inga", "inessa", "inna", "ira",
    "irina", "irine", "irma", "izabella", "jaklin", "jana", "janna", "juliet",
    "julieta", "karina", "karine", "katarina", "khatun", "xatun", "khatunik", "xatunik", "kristina", "kristine", "lara",
    "larisa", "laura", "lia", "liana", "lida", "lidiya", "lilit", "lilith",
    "lily", "liya", "liza", "louisa", "luiza", "lusine", "lusik", "madlen",
    "mane", "maneh", "margarit", "margarita", "mari", "maria", "mariam", "maryam",
    "mariana", "marianne", "marica", "marina", "marine", "maro", "marta", "mary",
    "maya", "meri", "milena", "mira", "monika", "monica", "nadezhda", "nadia",
    "naira", "nanar", "nane", "narek", "nare", "nareh", "natalia", "natali",
    "natella", "nazeli", "nazik", "nelli", "nelly", "nina", "nonna", "nune",
    "nunufar", "ofelya", "olga", "oksana", "parandzem", "polina", "raisa", "regina",
    "ripa", "ripsime", "rita", "roza", "rubina", "ruzanna", "ruzan", "sabina",
    "sahakanush", "salbi", "sarah", "sara", "satik", "satenik", "sedul", "seda",
    "shagane", "shnorhik", "shushan", "shushanik", "silva", "sirarpi", "sirun",
    "sirush", "sirusho", "sofia", "sofi", "sofya", "sona", "stell", "stella",
    "svetlana", "syuzi", "syuzanna", "suzanna", "talia", "tamara", "taguhi",
    "tanya", "tatev", "tatevik", "tatiana", "tsovinar", "valeria", "valida",
    "varditer", "varduhi", "varsik", "vartiter", "vartuhi", "varinka", "vera", "veronika",
    "viktoria", "victoria", "violeta", "violetta", "yeva", "yana", "yulia",
    "yuliya", "zabel", "zarine", "zaruhi", "zepur", "zhanna", "zhanna", "alina",
    "amalia", "anahit", "catherine", "chloe", "clara", "elena", "emily", "grace",
    "julia", "kate", "katia", "katya", "lea", "leah", "maya", "mia", "natalie",
    "nicole", "olivia", "rose", "sophie", "valerie", "yasmin", "zoe",
}

# Names known to be unisex, ambiguous, or shared across cultures
AMBIGUOUS_NAMES: Set[str] = {
    "sasha", "alex", "zhenya", "valya", "slava", "roma", "danya", "nikita",
    "andrea", "morgan", "sam", "taylor", "jordan", "rene", "rené", "camille",
    "dani", "robin", "shannon",
}


def extract_firstname_from_email(email: str) -> Optional[str]:
    """Extract clean first name token from email address.
    
    AUA student emails follow firstname_lastname@edu.aua.am or firstname.lastname@edu.aua.am.
    Trailing numbers, middle initials, or symbols are cleanly stripped.
    """
    if not email or "@" not in email:
        return None

    local_part = email.split("@")[0].strip().lower()
    # Split by underscore, dot, or hyphen
    parts = [p for p in re.split(r"[._\-]", local_part) if p]
    if not parts:
        return None

    first_token = parts[0]
    # Strip any digits or non-alphabetic characters
    cleaned = re.sub(r"[^a-z]", "", first_token)
    return cleaned if cleaned else None


def normalize_name(name: str) -> str:
    """Normalize transliteration variants common in Armenian names."""
    n = name.lower().strip()
    # Normalize common digraphs
    n = n.replace("ou", "u")

    # Armenian 'kh' (letter Խ) is frequently written with Russian/informal 'x'
    # (e.g. xachatur -> khachatur, xoren -> khoren, xosrov -> khosrov, ishxan -> ishkhan, mxitar -> mkhitar)
    if n.startswith("x"):
        n = "kh" + n[1:]
    n = n.replace("shx", "shkh")
    n = n.replace("mx", "mkh")
    n = n.replace("rx", "rkh")
    n = n.replace("lx", "lkh")
    n = n.replace("nx", "nkh")
    if n.endswith("xan"):
        n = n[:-3] + "khan"
    return n


def detect_gender(name: str) -> str:
    """Detect gender for a given first name.
    
    Returns:
        "male" | "female" | "unknown"
    """
    if not name:
        return "unknown"

    raw = name.lower().strip()
    norm = normalize_name(raw)

    if raw in AMBIGUOUS_NAMES or norm in AMBIGUOUS_NAMES:
        return "unknown"

    # Direct match in sets
    if raw in MALE_NAMES or norm in MALE_NAMES:
        return "male"
    if raw in FEMALE_NAMES or norm in FEMALE_NAMES:
        return "female"

    # Armenian diminutive suffix heuristics (e.g. -ik for males: Garnik, Razmik, Vanik, Babik)
    # Check if ends with typical male Armenian patterns
    if (raw.endswith("ik") or raw.endswith("ig")) and len(raw) >= 5:
        # Check against common female -ik exceptions (Astghik, Tatevik, Anushik, Hasmik, Arpik, Varsik)
        if raw in {"astghik", "tatevik", "anushik", "hasmik", "arpik", "varsik", "shushanik", "nazik", "satenik", "satik"}:
            return "female"
        return "male"

    # Names ending in -uhi are strictly female (Varduhi, Taguhi, etc.)
    if raw.endswith("uhi"):
        return "female"

    # Names ending in -ine are almost exclusively female (Lusine, Armine, Karine, Hermine, Marine)
    if raw.endswith("ine"):
        return "female"

    return "unknown"


def infer_gender_from_email(email: str) -> Tuple[Optional[str], str]:
    """Extract first name and infer gender from an AUA student email.
    
    Returns:
        (extracted_name, detected_gender)
        e.g. ("gor", "male"), ("ani", "female"), ("unknown_name", "unknown")
    """
    first_name = extract_firstname_from_email(email)
    if not first_name:
        return None, "unknown"

    gender = detect_gender(first_name)
    return first_name, gender
