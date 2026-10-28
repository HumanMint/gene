"""Bounded personal notes, never permissions, shared server facts, or worker input."""

import re

from .privacy import clean

CATEGORIES = ('nickname', 'games', 'music', 'hobbies', 'conversation', 'projects')
CONVERSATION_ONLY = ('projects',)  # Kept where it was said; only the member's own Share puts one in their profile.
MAX_NOTES = 10
PER_CATEGORY = 3
TTL = 90 * 86400
QUOTED = ('"', "'", '“', '‘', '>', '`')
SENTENCE = re.compile(r'[!?;\n]+|(?<![.\d])\.(?![.\d])')
# Common fillers that only look like a denial or a report: "not gonna lie, i like chess".
FILLER = re.compile(r"(?i)\b(?:not (?:gonna|going to) lie|not to brag|no (?:cap|lie|offen[cs]e)|"
                    r"(?:gotta|got to|have to|must|needless to) say|if (?:i'm|im|i am) honest|if you ask me)\b")
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
RETRACTED = re.compile(r"(?i)\b(?:jk|j/k|kidding|anymore|not really)\b|(?:^|\s)/s\b")


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
    spots = [m.start() for m in re.finditer(f'(?={re.escape(evidence)})', text) if whole(text, m.start(), evidence)]
    if not spots or not all(claimed(text, at, evidence) for at in spots):
        return None
    # What a member is doing is only ever a projects note, and a projects note only ever says what they're doing.
    if candidate['category'] == 'projects':
        if not re.match(r"(?i)^(?:i(?:['’]m| am) (?:working on|building)|i main|i['’]ve been playing)\b", evidence):
            return None
    elif not re.match(r"(?i)^(?:i (?:like|love|prefer|enjoy|play|usually|mostly)|my (?:favorite|favourite|nickname)|call me)\b", evidence):
        return None
    # Defense in depth; notes are still untrusted data even after validation.
    if re.search(r'(?i)\b(?:ignore|instructions?|system|developer|password|token|secret|address|diagnos\w*|'
                 r'politic\w*|religio\w*|sexual\w*|health|medical|permission|approv\w*|deploy|execute)\b', evidence):
        return None
    return candidate['category'], evidence


def whole(text, at, evidence):
    """Starts and ends where words do: not the "i like" in "wasabi like" or the "call me dan" in "call me danny"."""
    end = at + len(evidence)
    return not ((at and re.match(r"[\w'’-]", text[at - 1]))
                or (re.match(r'\w', evidence[-1]) and re.match(r'\w', text[end:end + 1])))


def claimed(text, at, evidence):
    """Whether the words at text[at:] are the member's own claim, not quoted, reported, denied, asked or taken back."""
    before, after = text[:at].replace('’', "'"), text[at + len(evidence):].replace('’', "'")
    if (before.rstrip().endswith(QUOTED) or before.rsplit('\n', 1)[-1].lstrip().startswith('>')
            or re.search(r'(?m)^\s*>>>', before) or before.count('`') % 2 or before.count('"') % 2
            or before.count('“') > before.count('”') or after.lstrip()[:1] in ('"', '”') or re.match(r"\s*'(?!\w)", after)):
        return False  # Quoted, code or a blockquote.
    *earlier, lead = SENTENCE.split(before)
    prior = next((part for part in reversed(earlier) if part.strip()), '')
    if re.search(r"(?i)(?:\b(?:said|says|wrote|writes|texted|asked|goes|told me|tells me)\b\W*|:\s*)$", prior):
        return False  # "she said:" and then the words on their own line.
    lead = FILLER.sub(' ', lead)
    if FRAMED.search(lead) or NEGATED.search(lead) or re.search(r'\w:(?:\s|$)', lead):
        return False  # "she said honestly …", "dont ever …", "bob: …".
    rest = SENTENCE.split(after, maxsplit=1)[0]
    if RETRACTED.search(evidence + rest) or re.match(r'(?i)\W*(?:\w+\W+)?(?:said|says|asked|wrote|texted)\b', rest):
        return False  # "… jk", "… said no one ever".
    if evidence.lower().startswith('call me'):
        return bool(ASKING.search(lead)) and not re.match(r"(?i)call me\s+[\w'-]+(?:\s+[\w'-]+)?\s*,?\s+but\b",
                                                          evidence + rest)  # Not "call me crazy but …".
    words = re.findall(r"[\w']+", lead.lower())
    return not words or words[-1] not in ASKED


def memory_intent(text):
    """Handle direct privacy requests locally, without asking a model to authorize them."""
    text = text.lower().strip().replace('’', "'")
    text = re.sub(r'^(?:(?:hey|hi|yo|ok|okay|please)[,\s]+)*fri\b[,\s:]*', '', text)
    text = re.sub(r'[,\s]+fri[.!?]*$', '', text).strip(' .!?')
    text = re.sub(r'[,\s]+please$', '', text)
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
