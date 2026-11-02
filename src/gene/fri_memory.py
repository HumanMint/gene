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
# "have to say" drops a report verb, so it's a filler only when the member says it: after "i", after nothing but
# interjections from the sentence's start, or after a "but" or "and" with at most one interjection, as before (a
# subject there may still be someone else's: "my friends agree and also honestly have to say …"). "they lol have to
# say …" and "they, honestly, have to say …" stay reports. Each word is crossed once.
SAID = r"lol|lmao|haha|ok|okay|yeah|yea|ya|so|fri|bro|dude|hey|yo|oh|well|btw|also|honestly|tbh"
FILLER = re.compile(rf"(?i)(^\W*|\b(?:{STARTER})\W+|(?:^\W*|[^\w\s']\s*|\b(?:{STARTER})\W*)\b(?:also|honestly|tbh)\W+|"
                    rf"{ME})(?:not (?:gonna|going to) lie|not to brag|no (?:cap|lie|offen[cs]e)|"
                    rf"if (?:i'm|im|i am) honest|if you ask me)\b|"
                    rf"(^\W*(?:(?:{SAID})\W+)*|\b(?:but|and)\W+(?:(?:{SAID})\W+)?|{ME})"
                    rf"(?:gotta|got to|have to|must|needless to) say\b")
# Someone else's words, a supposition or a quotation: nothing after it in the sentence is the member's claim. Main's
# rule as it was (8aad087): the checks below only ever refuse more (Sol round 2 of #178, see claims).
FRAMED = re.compile(
    r"(?i)\b(?:say|says|said|saying|tells?|told|telling|ask(?:s|ed)?|wr(?:ite|ites|ote)|text(?:s|ed)|goes|"
    r"think(?:s|ing)?|thought|believe[sd]?|assume[sd]?|claim(?:s|ed)?|quot(?:e|es|ed|ing)|apparently|"
    r"supposedly|allegedly|pretend\w*|imagine|suppose[sd]?|if|unless|whether|wish(?:es|ed)?|"
    r"(?:am|is|are|was|were|be|been|im|'m|'s|'re)\s+(?:\w+\s+)?like)\b")
# One report vocabulary, every form, for both places a report can stand (gene #172): before the claim (see frames) and
# after it starts, where an inflected one is a report and a bare one is after someone else (HEARD).
BARE = r"say|tell|ask|write|text|think|believe|assume|claim|insist|reckon|swear|quote"
INFLECTED = (r"says|said|saying|tells|told|telling|asks|asked|asking|writes|wrote|written|writing|texts|texted|texting|"
             r"thinks|thought|thinking|believes|believed|believing|assumes|assumed|assuming|claims|claimed|claiming|"
             r"insists|insisted|insisting|reckons|reckoned|reckoning|swears|swore|sworn|swearing|quotes|quoted|quoting")
HEARSAY = r"according to|apparently|supposedly|allegedly"
VERB = re.compile(rf"(?i)\b(?:{BARE}|{INFLECTED})\b")
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
# A report anywhere after the claim starts, inside the evidence or after it, in any form FRAMED knows before one:
# "i like chess, my friend said", "… my friend keeps saying", "… or so she believes", "… people say". A bare verb
# ("say", "think") counts after someone else's plural; after "i" it's their own hedge. Main's rule as it was (8aad087).
REPORT = re.compile(
    r"(?i)\b(?:says|said|saying|tells|told|telling|asks|asked|asking|writes|wrote|writing|texts|texted|texting|"
    r"thinks|thought|thinking|believes|believed|believing|assumes|assumed|assuming|claims|claimed|claiming|"
    r"insists|insisted|insisting|reckons|reckoned|swears|swore|sworn|quotes|quoted|quoting|according to|"
    r"apparently|supposedly|allegedly)\b|"
    r"\b(?:they|people|everyone|everybody|folks|friends|ppl|others|y'all)\s+(?:(?:all|always|still|keep|often|"
    r"usually|just|really|also)\s+)?(?:say|tell|think|believe|assume|claim|insist|reckon|swear)\b")
# Beyond REPORT (gene #172), a report anywhere after the claim starts: any inflected form ("i like chess, my friend
# said", "… they are swearing", "… or so she believes", "… according to my brother"), or a bare verb after its subject,
# through any run of adverbs, auxiliaries or interjections ("… my parents say", "… the boys would say", "… my mom will
# tell you", "… or so ya still always really just say", gene #187). The subject is any noun phrase but the member's own
# (see OWN): "i like chess, i think" is their hedge. It's the word before the run, never one in it, so each run is
# crossed once, from its subject, and none of its words is read as one.
BETWEEN = (r"all|always|still|keep|often|usually|sometimes|just|really|also|even|actually|literally|basically|totally|"
           r"only|kinda|lowkey|would|will|can|could|should|might|may|must|do|does|did|gonna|wanna|gotta")
SPOKEN = BARE  # Every bare form: "… my friends write", "… my parents text" (Sol round 1 of #178).
# Fri and a friend may be someone's name as well as an interjection: before a report verb, they're someone else.
NAMES = frozenset({'fri', 'bro', 'dude'})
# Interjections that may sit between a subject and its verb ("my mom lol would say"); never "ya", which may be "you".
ASIDES = '|'.join(word for word in SAID.split('|') if word not in NAMES | {'ya'})
MODIFIER = rf"(?:{BETWEEN}|{ASIDES})"
HEARD = re.compile(rf"(?i)\b(?:{INFLECTED}|{HEARSAY})\b|"
                   rf"(?<![\w'])(?!{MODIFIER}(?![\w']))(?P<subject>[\w']+)\s+(?:{MODIFIER}\s+)*+(?:{SPOKEN})\b")
# Main's subjects count wherever they stand ("i like chess they say"); any other only after a break in the claim, so its
# own words never do: "i like chess, my mom lol would say" is a report, "i love jazz lol say what you want" isn't.
PLURALS = frozenset({'they', 'people', 'everyone', 'everybody', 'folks', 'friends', 'ppl', 'others', "y'all"} |
                    # Whoever the member speaks to, however it's spelled, counts like y'all (gene #187).
                    {'you', 'u', 'ya', 'yall', 'guys'})
# The member's own asides to whoever they speak to, never a report: "… if you ask me", "…, makes you think".
YOU = r"(?:you guys|you|u|ya|yall|y'all)"  # Every spelling, in each place (gene #187).
ASIDE_PHRASES = re.compile(rf"(?i)\bif {YOU} (?:ask|asked) me\b|\bmakes? {YOU} (?:think|wonder)\b")
# After the claim, a report form in the member's own clause with something of its own after it: "…, ive written a
# song", "…, im swearing at the lag" (S1). Not one that points back at the claim ("…, i said", "…, i told you so") or
# is passive ("…, or so i was told").
ME_CLAUSE = re.compile(rf"(?i)(?:^|[^\w\s']\s*|\b(?:and|but|so|cuz|coz|because|while|though|tho)\s+)(?:(?:{ASIDES})\s+)*"
                       r"(?:i|i'm|im|i've|ive|i'd|i'll|we|we're|we've|we'd)\s+(?:(?:was|were|am|are|have|had|been|"
                       r"keep|kept|just|really|also|still|always|even|actually|literally|kinda|lowkey)\s+){0,3}$")
# Forms that may be an activity, not a report: "…, ive written it down" is the member's, "…, i said it" points back.
ACTIVITY = frozenset({'writes', 'wrote', 'written', 'writing', 'texts', 'texted', 'texting'})
BACKREF = frozenset('it that this so as you u ya before already earlier again too once twice yesterday everyone '
                    'everybody them him her ppl people'.split())
# "who would think", "who'd say otherwise": a rhetorical question of the member's, not someone's report.
RHETORICAL = re.compile(r"(?i)who(?:'d|\s+(?:would|could|might|will|can)\b)")
BREAK = re.compile(r"(?i)[^\w\s']|\b(?:and|but|or|cuz|coz|because|as|while|since|though|plus|who|when)\b")
# Before a bare report verb, words that make it no one else's report: the member ("i", "we"), a describing "that" or
# "which", and words that start no clause of someone else's ("needless to say", "never say never", "a say", "let me
# tell you", "lol say it"). Whoever the member speaks to is someone else ("…, you say"), short of the member's own
# asides to them (ASIDE_PHRASES).
OWN = frozenset(
    "i me my mine myself im ive we us our ours ourselves "
    "that which a an the this his her its their to and or but nor not never no dont doesnt didnt cant cannot wont "
    "wouldnt shouldnt couldnt aint isnt arent wasnt werent let lets please pls plz go come if as than then when "
    f"{SAID.replace('|', ' ')} {BETWEEN.replace('|', ' ')}".split()) - {'ya'}  # "or so ya say" (B4).


# Before the claim, a report verb keeps the note only where its clause is positively the member's own (Sol round 1 of
# #178: fail closed). Its clause starts after punctuation, one of these, or the sentence's start.
STARTS = re.compile(r"(?i)[^\w\s']|\b(?:but|so|anyway|then|cuz|coz|because|since|though|tho|while)\b")
ME = frozenset({'i', "i'm", 'im', "i've", 'ive', "i'd", "i'll", 'we', "we're", "we've", "we'd", "we'll"})
# What may sit between the member and their verb: "i was saying", "i keep telling", "i've been thinking", and what they
# do as an activity: "i enjoy writing songs", "we are writing a song".
ME_BETWEEN = frozenset({'was', 'were', 'am', 'are', 'have', 'had', 'been', 'keep', 'kept', 'just', 'really', 'also',
                        'still', 'always', 'even', 'actually', 'literally', 'totally', 'kinda', 'lowkey', 'would', 'will',
                        'can', 'could', 'should', 'might', 'must', 'do', 'did', 'gonna', 'wanna', 'gotta', 'enjoy',
                        'love', 'like', 'hate', 'prefer', 'start', 'started', 'stop', 'stopped', 'quit', 'finished',
                        'tried', 'practice'})
# Between a passive's helper and its participle: "i was just told", "or so i've recently been told".
ADVERBS = frozenset({'just', 'really', 'also', 'already', 'always', 'still', 'even', 'actually', 'literally',
                     'recently', 'once', 'often', 'never', 'finally', 'lately', 'usually', 'sometimes', 'mostly'})
# A noun, not a verb, after these: "just a thought but …", "my text was long, …", and so is a gerund or a participle
# used as one ("my writing is messy, …", "the swearing was wild, …", "my sworn enemy, …", gene #188).
NOUNS = frozenset({'thought', 'text', 'quote', 'claim', 'say', 'texts', 'quotes', 'claims', 'written', 'sworn'})
OWN_NOUNS = frozenset({'a', 'an', 'the', 'my', 'your', 'our', 'his', 'her', 'their', 'this', 'that', 'some'})
# A gerund after these is an activity's name, not a report: "after writing all night, …", "tired of texting, …".
PREPOSITIONS = frozenset({'after', 'before', 'from', 'about', 'at', 'with', 'for', 'of', 'in', 'on', 'into', 'by'})
# A gerund that starts its clause before one of these is its subject: "writing is fun, …", "swearing makes it worse".
COPULAS = frozenset({'is', 'was', 'are', 'were', "isn't", "wasn't", 'has', 'had', 'can', 'could', 'will', 'would',
                     'might', 'should', 'makes', 'made', 'takes', 'took', 'gets', 'got', 'feels', 'felt', 'seems'})
PASSIVE = frozenset({'was', 'were', 'been', 'be', 'is', 'are', 'am', 'being', 'get', 'got', 'gets', 'getting'})
PARTICIPLES = frozenset({'said', 'told', 'written', 'asked', 'texted', 'thought', 'believed', 'assumed', 'claimed',
                         'insisted', 'reckoned', 'sworn', 'quoted'})
ASIDE_WORDS = frozenset(ASIDES.split('|'))
FORMS = frozenset(INFLECTED.split('|'))
# A clause of the member's own that only sits before the claim ends at punctuation or one of these.
JOINS = r"and|but|so|then|anyway|because|cuz|since|while|though|plus"
# Matched from just after a report verb to the claim: up to three plain words, or only punctuation and interjections,
# introduce it; "me" or "us" right after an inflected form is who it was told to.
INTRODUCES = re.compile(rf"(?i)\s+(?:(?!(?:{JOINS})\b)[\w']+\s+){{0,3}}")
POINTS_BACK = re.compile(rf"(?i)\s+(?:that|this|it|so)\W*(?:(?:{SAID})\W+)*")  # "i said that, i like …": quoting it.
ASIDE_ONLY = re.compile(rf"(?i)\W*(?:(?:{SAID})\W+)*")
TOLD = re.compile(r"(?i)\s+(?:to\s+)?(?:me|us)\b")
WORD = re.compile(r"[\w']+")
TOKEN = re.compile(r"[\w']+|[^\w\s]")


def own_clause(lead, low, m):
    """Whether the clause a report verb before the claim stands in is positively the member's own: 'me' when it
    starts with them ("i was saying earlier, …", "lol i keep telling …", at most three helping words between), 'own'
    for a short list of theirs without an "i", in a clause that starts at punctuation or the sentence's start ("let
    me tell you, …", "believe me, …", "been writing all day, …", "stop swearing lol, …", "just a thought but …", "ask
    anyone, …"), else None: someone else's, or no one can tell, which keeps nothing ("said jo, …", "just said jo, …",
    whose "just" may be a name, "my mom, as usual, said that, …", "May said earlier, …"). What the member does
    counts, through adverbs ("i've recently been writing songs, …"), and so does a report word used as a noun ("my
    writing is messy, …", "writing is fun, …", "after writing all night, …"), gene #188. `low` is `lead` lowercased,
    once for every report verb in it (gene #188)."""
    verb, window = m.group().lower(), max(0, m.start() - 80)
    starts = list(STARTS.finditer(lead, window, m.start()))
    if not starts and window > 0:
        return None  # Its clause runs back past what's looked at.
    words = WORD.findall(low, starts[-1].end() if starts else 0, m.start())
    joined = bool(starts) and WORD.fullmatch(starts[-1].group())  # After "but", "so" …, which may be a name.
    lead_in = next((n for n, word in enumerate(words) if not (word in ASIDE_WORDS or word in NAMES) or word == 'ya'),
                   len(words))
    if lead_in < len(words) and words[lead_in] in ME:
        del words[:lead_in]  # "lol i was saying", "bro i was saying". Before anything else, any of them may be a
        # name ("So just said …", "bro just said …"), and "ya" may be "you": no member there.
    helping = [word for word in words[1:] if word not in ADVERBS]  # "i've recently been writing" (gene #188).
    if words and words[0] in ME and len(helping) <= 3 and all(word in ME_BETWEEN for word in helping):
        return 'me'
    if joined:
        return None  # Without an "i", only a clause that starts at punctuation or the sentence's start.
    tail = ' '.join(WORD.findall(low, m.end(), m.end() + 24)[:3])
    if (words == ['let', 'me'] or (not words and verb == 'believe' and (tail == 'me' or tail == 'it or not')) or
            (not words and verb == 'ask' and tail in ('anyone', 'anybody')) or
            (verb.endswith('ing') and words in (['been'], ['just', 'been'], ['stop'])) or
            ((verb in NOUNS or verb.endswith('ing')) and 0 < len(words) <= 2 and words[-1] in OWN_NOUNS) or
            (verb.endswith('ing') and words and words[-1] in PREPOSITIONS) or
            (verb.endswith('ing') and not words and tail.split(' ')[0] in COPULAS)):
        return 'own'
    return None


def frames(lead):
    """Whether a report verb before the claim, in its sentence, frames the claim as someone else's. Any whose clause
    isn't positively the member's own does (see own_clause). One of theirs does when it's passive ("i was told by my
    mom, …"), told to them ("just told me, …"), or introduces the claim, with nothing but punctuation and
    interjections or up to three plain words between ("i think i like chess", "i told jo i like …"); a participle
    only introduces it without an "i" (gene #172)."""
    if re.search(r'(?i)\baccording to\b', lead):
        return True  # Hearsay main's FRAMED doesn't name.
    low = lead.lower()  # Once, not once for each report verb (gene #188).
    for m in VERB.finditer(lead):
        own = own_clause(lead, low, m)
        if own is None:
            return True
        verb, after = m.group().lower(), m.end()
        before = [word for word in WORD.findall(low, max(0, m.start() - 64), m.start()) if word not in ADVERBS]
        previous = before[-1] if before else ''
        if verb in PARTICIPLES and (previous in PASSIVE or re.search(r"'(?:s|re|m)$|^im$", previous)):
            return True  # "i was told …", "i was just told …", "it's said …".
        if verb in FORMS and TOLD.match(lead, after):
            return True  # "just told me, …", "said to me …".
        words = INTRODUCES.fullmatch(lead, after)
        if verb.endswith('ing'):
            if words and own != 'me':
                return True  # A participle that introduces it: "been saying i like chess".
            continue  # What the member was doing: "been writing all day, …", "i keep saying …".
        if words or ASIDE_ONLY.fullmatch(lead, after) or POINTS_BACK.fullmatch(lead, after):
            return True
    return False


def independent(text, low, m):
    """An inflected report form after the claim in the member's own clause, with something of its own after it (see
    ME_CLAUSE). `low` is `text` lowercased, once for every form in it (gene #188)."""
    if not ME_CLAUSE.search(text, max(0, m.start() - 60), m.start()):
        return False
    verb = m.group().lower()
    before = [word for word in WORD.findall(low, max(0, m.start() - 48), m.start()) if word not in ADVERBS]
    if verb in PARTICIPLES and before and (before[-1] in PASSIVE or re.search(r"'(?:s|re|m)$|^im$", before[-1])):
        return False  # "…, or so i was just told", "…, i'm told".
    following = WORD.findall(low, m.end(), m.end() + 40)[:2]
    if not following or following[0] in ASIDE_WORDS:
        return False
    if following[0] in BACKREF:  # Pointing back at the claim, unless an activity goes on: "ive written it down".
        return verb in ACTIVITY and len(following) > 1 and following[0] in ('it', 'that', 'this') and \
            following[1] not in ASIDE_WORDS | BACKREF
    return True


# A clause of nothing but these (and a whole courtesy clause) leaves a condition before it open: "if i win, then".
UNFINISHED = ASIDE_WORDS | {'please', 'pls', 'plz', 'then', 'and', 'but', 'or', 'plus', 'anyway', 'tho', 'though'}
OPENS = re.compile(rf"(?i)(?:(?:{'|'.join(sorted(UNFINISHED))})\s+)*(?:unless|when|whenever|once|until|till|as long as|"
                   r"provided|if)\b")


# A clause that goes on from the one before it ("and finish the match early", "or tie") answers nothing: whether a
# condition is open is up to the clause or line before it (gene #186).
CONTINUES = frozenset({'and', 'or', 'nor', 'plus'})


def unresolved(line):
    """Whether a line ends inside a condition (True), answers it or says something else (False), or has no clause
    of its own words at all (None): only a courtesy, interjections, "then" or clauses that go on from the one before
    ("if i win\nand finish the match early\n…", gene #186)."""
    for clause in reversed(re.split(r"[^\w\s']+", line)):
        words = WORD.findall(COURTESY.sub('', clause.strip(), count=1).lower())
        if any(word not in UNFINISHED for word in words):
            opened = bool(OPENS.match(' '.join(words)))
            first = next((word for word in words if word not in ASIDE_WORDS), '')
            if opened or first not in CONTINUES:
                return opened
    return None


def reported(spaced):
    """Whether the claim (from the evidence to its sentence's end, spaces joined) holds a report HEARD knows that
    REPORT doesn't: an inflected form outside a description or the member's own independent clause, or a bare verb
    after someone else's subject (main's plurals anywhere, any other past a break), short of the member's asides."""
    broken = (BREAK.search(spaced) or re.search('$', spaced)).start()  # The claim's own words end here at most.
    heard = ASIDE_PHRASES.sub(lambda aside: ' ' * len(aside.group()), spaced)  # Same places, asides blanked.
    low = heard.lower()
    return any(reported_by(m['subject']) and not RHETORICAL.match(m.group()) and (
                   m.start() > broken or m['subject'].lower() in PLURALS) if m['subject'] else
               not DESCRIBED.search(heard[max(m.start() - 48, 0):m.start()]) and not independent(heard, low, m)
               for m in HEARD.finditer(heard))


def reported_by(subject):
    """Whether a bare report verb after this word is someone else's: the word without its contraction ("they'd") is a
    name (NAMES), or isn't one of OWN, nor is it with its apostrophe dropped ("don't")."""
    word = re.sub(r"'(?:d|ll|re|ve|s|m)$", '', subject.lower())
    return word in NAMES or (word not in OWN and word.replace("'", '') not in OWN)


# "a bot that tells jokes", "… which sometimes says it", "… that's always telling jokes", "… that keeps saying gm"
# only describe something.
DESCRIBED = re.compile(r"(?i)\b(?:that|which)(?:'s|\s+(?:is|are|was|were))?\s+(?:(?:always|sometimes|often|usually|"
                       r"never|just|also|really|actually|literally|basically|still|only|even|kinda|lowkey|keeps?)\s+)?$")
# A nickname only on a condition isn't one: "call me dan if i win", wherever the evidence stops. A whole courtesy
# clause that ends the claim only asks nicely ("if you want", "whenever you want", "if that's ok", "if that makes
# sense"); "if you like chess" and "if you want to win" are conditions.
CONDITION = re.compile(r"(?i)\b(?:unless|when|whenever|once|until|till|as long as|provided|if)\b")
COURTESY = re.compile(r"(?i)\b(?:(?:if|whenever|when) (?:you|u) (?:want(?: to)?|wanna|like|prefer|'?d like|would like|"
                      r"feel like it|don'?t mind)|if (?:that'?s|it'?s) (?:ok|okay|cool|fine|alright)|if (?:that|it) makes sense)"
                      r"\W*(?:(?:lol|lmao|haha|tho|though|bro|dude|fri|please|pls|plz|ok|okay)\W*)*$")
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

    def open_condition(self, k):
        """A condition the lines before sentence k leave open, joined to it by line breaks alone: the last clause
        with words of its own starts with one ("whenever we play\nplease call me dan", "if i win, then\n…"). Lines and
        clauses of nothing but a courtesy, interjections or "then" are crossed ("if i win\nif that's ok\n…"); a
        consequent resolves it ("i lost when we played\n…") (Sol rounds 1 and 2 of #178)."""
        t, j = self.text, k - 1
        while j >= 0 and not set(t[self.ends[j]:self.stops[j]]) - {'\n', '\r', ' ', '\t'}:
            found = unresolved(t[self.starts[j]:self.ends[j]])
            if found is not None:
                return found
            j -= 1  # Blank, a courtesy or filler only: the line before it decides.
        return False

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
        spaced = re.sub(r'\s+', ' ', claim)
        if (RETRACTED.search(claim) or any(not DESCRIBED.search(spaced[max(m.start() - 48, 0):m.start()]) for m in REPORT.finditer(spaced))
                or TAKEN_BACK.fullmatch(following) or re.match(r'(?i)\W*(?:\w+\W+)?(?:said|says|asked|wrote|texted)\b', rest)):
            return False  # "… jk", "… said no one ever", "… my friend said", "… . jk".
        if (CONDITION.search(COURTESY.sub('', spaced, count=1))
                and evidence.lower().startswith(('call me', 'my nickname'))):
            return False  # "call me dan if i win", "my nickname is dan when i stream".
        # Everything above is main's rule as it was (8aad087). What follows only ever refuses more (Sol round 2 of
        # #178), so nothing main refuses is kept: gene #172's reports and #173's conditions.
        if frames(lead) or reported(spaced):
            return False  # "she insists i like chess", "… my parents say", "… they are swearing".
        if (evidence.lower().startswith(('call me', 'my nickname'))
                and (CONDITION.search(COURTESY.sub('', re.sub(r'\s+', ' ', lead), count=1)) or self.open_condition(k))):
            return False  # "whenever we play, call me dan", "whenever we play\ncall me dan".
        if call:
            return bool(ASKING.search(lead)) and not IDIOM.match(claim)
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
