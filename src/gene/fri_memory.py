"""Bounded personal notes, never permissions, shared server facts, or worker input."""

import bisect
import re

from .privacy import clean

CATEGORIES = ('nickname', 'games', 'music', 'hobbies', 'conversation', 'projects')
CONVERSATION_ONLY = ('projects',)  # Kept where it was said; only the member's own Share puts one in their profile.
MAX_NOTES = 10
PER_CATEGORY = 3
TTL = 90 * 86400
MAX_SPOTS = 8  # The same words more often than this in one message keep nothing.
QUOTED = ('"', "'", '“', '‘', '>', '`')
SENTENCE = re.compile(r'[!?;\n]+|(?<![.\d])\.(?![.\d])')
# Single quotes open after a non-word and close before one; "don't", "i'm", "'cause" and "'90s" are neither.
SINGLE = re.compile(r"(?<!\w)(['‘])(?=\S)(?!(?i:cause|cuz|coz|bout|til|till|em|n|round)\b|\d\ds?\b)|(?<=\S)'(?!\w)")
# A sentence ending in "she said:", so the next one is her words.
PRIOR = re.compile(r"(?i)(?:\b(?:said|says|wrote|writes|texted|asked|goes|told me|tells me)\b\W*|:\s*)$")
# Common fillers that only look like a denial or a report, on their own or after "i": "not gonna lie, i like
# chess", "i have to say i like chess". An aside ("not gonna lie", "if you ask me") reports no one else's words, so
# it may follow an interjection anywhere; "also", "honestly" and "tbh" start one only where a clause does. After
# punctuation only spaces may follow, so a run of commas is crossed once, not once from each comma.
STARTER = r"lol|lmao|haha|ok|okay|yeah|yea|ya|so|and|but|fri|bro|dude|hey|yo|oh|well|btw"
ME = r"\b(?:i|i'd|i'll|i'm|im|i am|i've|ive)\s+(?:(?:really|just|honestly)\s+)?"
# "have to say" drops a report verb, so it's a filler only when the member says it: after "i", or with nothing but
# interjections before it back to the sentence's start or a "but" or "and". "they lol have to say …" and "they,
# honestly, have to say …" stay reports. "but" and "and" only start the run, so each word is crossed once.
SAID = r"lol|lmao|haha|ok|okay|yeah|yea|ya|so|fri|bro|dude|hey|yo|oh|well|btw|also|honestly|tbh"
FILLER = re.compile(rf"(?i)(^\W*|\b(?:{STARTER})\W+|(?:^\W*|[^\w\s']\s*|\b(?:{STARTER})\W*)\b(?:also|honestly|tbh)\W+|"
                    rf"{ME})(?:not (?:gonna|going to) lie|not to brag|no (?:cap|lie|offen[cs]e)|"
                    rf"if (?:i'm|im|i am) honest|if you ask me)\b|"
                    rf"((?:^\W*|\b(?:but|and)\W+)(?:(?:{SAID})\W+)*|{ME})(?:gotta|got to|have to|must|needless to) say\b")
# Someone else's words, a supposition or a quotation: nothing after it in the sentence is the member's claim.
FRAMED = re.compile(
    r"(?i)\b(?:say|says|said|saying|tells?|told|telling|ask(?:s|ed)?|wr(?:ite|ites|ote)|text(?:s|ed)|goes|"
    r"think(?:s|ing)?|thought|believe[sd]?|assume[sd]?|claim(?:s|ed)?|quot(?:e|es|ed|ing)|apparently|"
    r"supposedly|allegedly|pretend\w*|imagine|suppose[sd]?|if|unless|whether|wish(?:es|ed)?|"
    r"(?:am|is|are|was|were|be|been|im|'m|'s|'re)\s+(?:\w+\s+)?like)\b")
# A denial anywhere earlier in the sentence, however many words sit between: "dont ever call me dan".
NEGATED = re.compile(r"(?i)\b(?:not|never|no(?!\s*,)|nobody|noone|nothing|neither|nor|without|cannot|ain'?t|"
                     r"\w+n't|(?:do|does|did|is|are|was|were|ca|could|wo|would|should|have|has|had)nt)\b")
# Right before "i like" or "my favorite", a question: "should i play", "what's my nickname".
ASKED = ('do', 'does', 'did', 'can', 'could', 'will', 'would', 'shall', 'should', 'may', 'might', 'must',
         'what', 'whats', "what's")
# "call me" is the member's own ask after nothing, a filler or "you can"; "they call me" isn't.
ASKING = re.compile(r"(?i)(?:^|\b(?:please|pls|plz|just|so|and|also|now|actually|instead|then|honestly|tbh|ngl|"
                    r"btw|lol|lmao|haha|ok|okay|yeah|yea|ya|yes|oh|hey|hi|yo|fri|bro|dude|can|could|will|may|"
                    r"should|from now on|(?:can|could|will|would) (?:you|u)|(?:you|u) to))\W*$")
# "can you call me dan?" asks to be called it, the one question that keeps a note.
POLITE = re.compile(r"(?i)\W*(?:(?:lol|lmao|haha|ok|okay|hey|hi|yo|fri|so|also|and|bro|dude|btw)\W+)*"
                    r"(?:can|could|would|will) (?:you|u)(?: (?:please|pls|plz|just))*\W*")
RETRACTED = re.compile(r"(?i)\b(?:jk|j/k|kidding|anymore|not really)\b|(?:^|\s)/s\b")
# A report anywhere after the claim starts, inside the evidence or after it: "i like chess, my friend said". "a bot
# that tells jokes" only describes something.
REPORT = re.compile(r"(?i)(?<!\bthat )(?<!\bwhich )\b(?:said|says|asked|asks|wrote|writes|texted|texts|told|tells|"
                    r"claims|claimed|thinks)\b")
# A nickname only on a condition isn't one: "call me dan if i win", wherever the evidence stops. "if you want" and
# "if that's ok" only ask nicely.
CONDITION = re.compile(r"(?i)\b(?:unless|when|whenever|once|until|till|as long as|provided|if(?! (?:you|u) (?:want|wanna|"
                       r"like|don'?t mind)\b| (?:that'?s|it'?s) (?:ok|okay|cool|fine|alright)\b))\b")
# The next sentence, on its own, takes it back: "i like chess. jk", "i like chess\nlol no".
TAKEN_BACK = re.compile(r"(?i)\W*(?:(?:lol|lmao|lmfao|haha)\W+)*(?:jk|j/k|/s|just kidding|kidding|not really|"
                        r"not anymore|no|nah|nope)(?:\W+(?:lol|lmao|lmfao|haha))*\W*")
# "call me crazy (but …)" is an idiom, not a nickname; "call me dan, but not danny" is one.
IDIOM = re.compile(r"(?i)call me\s+(?:an?\s+)?(?:crazy|insane|nuts|mad|weirdo?|old[- ]fashioned|paranoid|nerdy?|geek|"
                   r"stupid|dumb|idiot|naive|picky|petty|biased|basic|cringe|lazy|snob|hater|boomer|romantic|"
                   r"sentimental|cynical|sucker|extra|dramatic|hypocrite|cheap|whatever|anything|out)\b")

# The starts a note may have, as members type them: "im" and "ive" without the apostrophe, "fav" and "fave",
# and at most one everyday adverb before the verb ("i really like", "im still working on"). Never "i'm into".
ADVERB = r"(?:(?:really|also|kinda|lowkey|mostly|still|just) )?"
DOING = re.compile(rf"(?i)^(?:i(?:['’]?m| am) {ADVERB}(?:working on|building)|i main|i['’]?ve {ADVERB}been playing)\b")
LIKES = re.compile(rf"(?i)^(?:i {ADVERB}(?:like|love|prefer|enjoy|play|usually|mostly)|my (?:fav(?:e|ou?rite)?s?|nickname)|"
                   r"call me)\b")


def personal_note(candidate, text):
    """Keep a short exact self-disclosure, not a model-written biography/instruction."""
    if not isinstance(candidate, dict) or candidate.get('category') not in CATEGORIES:
        return None
    evidence = candidate.get('evidence', '')
    if not isinstance(evidence, str) or not 5 <= len(evidence) <= 180 or evidence not in text:
        return None
    if clean(evidence, 180) != evidence or any(s in evidence for s in ('\n', '`', '<@', '://')):
        return None
    # Every place these words stand as whole words must be the member's own plain claim; any doubt keeps nothing.
    spots, at = [], text.find(evidence)
    while at >= 0 and len(spots) <= MAX_SPOTS:
        if whole(text, at, evidence):
            spots.append(at)
        at = text.find(evidence, at + 1)
    if not spots or len(spots) > MAX_SPOTS:
        return None
    source = Source(text)
    if not all(source.claims(at, evidence) for at in spots):
        return None
    # What a member is doing is only ever a projects note, and a projects note only ever says what they're doing.
    if not (DOING if candidate['category'] == 'projects' else LIKES).match(evidence):
        return None
    # Defense in depth; notes are still untrusted data even after validation.
    if re.search(r'(?i)\b(?:ignore|instructions?|system|developer|password|token|secret|address|diagnos\w*|'
                 r'politic\w*|religio\w*|sexual\w*|health|medical|permission|approv\w*|deploy|execute)\b', evidence):
        return None
    return candidate['category'], evidence


def whole(text, at, evidence):
    """Starts and ends where words do: not the "i like" in "wasabi like" or the "call me dan" in "call me danny".
    An opening quote starts one ("'i like …"), so the quote check sees it; the apostrophe in "don't" doesn't."""
    end = at + len(evidence)
    return not (re.search(r"(?:[\w-]|\w['’])$", text[max(at - 2, 0):at])
                or (re.match(r'\w', evidence[-1]) and re.match(r'\w', text[end:end + 1])))


class Source:
    """One read of a member's message: its sentences, lines and quotes, so no occurrence rescans it."""

    def __init__(self, text):
        self.text = t = text.replace('’', "'")
        # Sentence k is starts[k]:ends[k], then its terminator up to stops[k].
        found = list(SENTENCE.finditer(t))
        self.starts = [0] + [m.end() for m in found]
        self.ends = [m.start() for m in found] + [len(t)]
        self.stops = [m.end() for m in found] + [len(t)]
        self.filled = [k for k, (a, b) in enumerate(zip(self.starts, self.ends)) if t[a:b].strip()]
        self.reported = [bool(PRIOR.search(t[self.starts[k]:self.ends[k]])) for k in self.filled]
        # Whether each line is a blockquote or follows a ">>>" one.
        self.lines, self.blocked, deep = [0] + [m.end() for m in re.finditer('\n', t)], [], False
        for a, b in zip(self.lines, self.lines[1:] + [len(t)]):
            line = t[a:b].lstrip()
            deep = deep or line.startswith('>>>')
            self.blocked.append(deep or line.startswith('>'))
        self.marks = {c: [m.start() for m in re.finditer(re.escape(c), t)] for c in '`"“”'}
        found = list(SINGLE.finditer(t))
        self.quotes, self.opens = [m.start() for m in found], [bool(m.group(1)) for m in found]

    def claims(self, at, evidence):
        """Whether the words at `at` are the member's own claim, not quoted, reported, denied, asked or taken back."""
        t, end = self.text, at + len(evidence)
        k = bisect.bisect_right(self.stops, at)
        if bisect.bisect_right(self.stops, end - 1) != k:
            return False  # Within one sentence: "i like chess. no" is two.
        seen = {c: bisect.bisect_left(found, at) for c, found in self.marks.items()}
        quote, after = bisect.bisect_left(self.quotes, at), t[end:].lstrip()
        if (t[:at].rstrip().endswith(QUOTED) or self.blocked[bisect.bisect_right(self.lines, at) - 1]
                or seen['`'] % 2 or seen['"'] % 2 or seen['“'] > seen['”'] or (quote and self.opens[quote - 1])
                or after[:1] in ('"', '”') or re.match(r"'(?!\w)", after)):
            return False  # Quoted, code or a blockquote.
        lead = FILLER.sub(r'\1\2 ', t[self.starts[k]:at])
        rest = t[end:self.ends[k]]  # Empty when the evidence takes in the terminator.
        before = bisect.bisect_left(self.filled, k)
        if ((before and self.reported[before - 1]) or FRAMED.search(lead) or NEGATED.search(lead)
                or re.search(r'\w:(?:\s|$)', lead)):
            return False  # "she said:" the line before, "she said honestly …", "dont ever …", "bob: …".
        call = evidence.lower().startswith('call me')
        if '?' in t[self.ends[k]:self.stops[k]] and not (
                call and POLITE.fullmatch(lead)
                and re.fullmatch(r'(?i)\W*(?:(?:please|pls|plz|from now on|instead|now)\W*)*', rest)):
            return False  # "i like chess?" asks.
        claim, later = t[at:end] + rest, bisect.bisect_right(self.filled, k)
        following = (t[self.starts[self.filled[later]]:self.ends[self.filled[later]]]
                     if later < len(self.filled) else '')
        # A report anywhere in the claim, and right after the evidence any verb a word away, even "that says".
        if (RETRACTED.search(claim) or REPORT.search(claim) or TAKEN_BACK.fullmatch(following)
                or re.match(r'(?i)\W*(?:\w+\W+)?(?:said|says|asked|wrote|texted)\b', rest)):
            return False  # "… jk", "… said no one ever", "… my friend said", "… . jk".
        if call:
            return bool(ASKING.search(lead)) and not IDIOM.match(claim) and not CONDITION.search(claim)
        words = re.findall(r"[\w']+", lead.lower())
        return not words or words[-1] not in ASKED


def memory_intent(text):
    """Handle direct privacy requests locally, without asking a model to authorize them."""
    text = text.lower().strip().replace('’', "'")
    text = re.sub(r'^(?:(?:hey|hi|yo|ok|okay|please)[,\s]+)*fri\b[,\s:]*', '', text)
    # Each suffix starts where its separator run does, so a long run isn't retried from every spot inside it.
    text = re.sub(r'(?<![,\s])[,\s]+fri[.!?]*$', '', text).strip(' .!?')
    text = re.sub(r'(?<![,\s])[,\s]+please$', '', text)
    text = re.sub(r'^(?:please |can you |could you )', '', text).strip(' .!?')
    if text in ('what do you remember about me', 'what do you know about me', 'show my memory',
                'show me my memory', 'show my memories', 'what have you remembered about me'):
        return 'show'
    if text in ('forget me', 'forget everything about me', 'forget what you know about me',
                'forget what you remember about me', 'clear my memory', 'clear my memories',
                'stop remembering me', "don't remember me", 'turn memory off', 'disable memory'):
        return 'disable'
    if text in ('remember me again', 'start remembering me', 'turn memory on', 'enable memory'):
        return 'enable'
    return None


def describe_memory(profile):
    state = 'on' if profile['enabled'] else 'off'
    notes = profile['notes']
    body = '\n'.join('- ' + note['evidence'] for note in notes) or 'No personal notes saved for this conversation yet.'
    return (f'Memory is {state}. {"Here’s what I’ve kept from what you told me:" if notes else ""}\n{body}\n\n'
            'I keep up to ten small notes here, at most three per category, for 90 days, and six recent exchanges for seven days. '
            'Use My memory to edit or remove one preference. '
            'Private chats and other channels stay separate. Say “forget me” to erase our saved chats '
            'and personal notes everywhere and turn memory off, or “remember me again” to turn it on.')
