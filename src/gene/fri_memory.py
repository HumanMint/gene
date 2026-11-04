"""Bounded personal notes, never permissions, shared server facts, or worker input."""

import bisect
import functools
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
ASIDE_PHRASES = re.compile(r"(?i)\bif (?:you|u|ya) (?:ask|asked) me\b|\bmakes? (?:you|u|ya) (?:think|wonder)\b")
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
                     'recently', 'once', 'often', 'never'})
# A noun, not a verb, after these: "just a thought but …", "my text was long, …".
NOUNS, OWN_NOUNS = frozenset({'thought', 'text', 'quote', 'claim', 'say'}), frozenset({'a', 'my'})
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
    whose "just" may be a name, "my mom, as usual, said that, …", "May said earlier, …"). `low` is `lead`
    lowercased, once for every report verb in it (gene #188's nit)."""
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
    if words and words[0] in ME and len(words) <= 4 and all(word in ME_BETWEEN for word in words[1:]):
        return 'me'
    if joined:
        return None  # Without an "i", only a clause that starts at punctuation or the sentence's start.
    tail = ' '.join(WORD.findall(low, m.end(), m.end() + 24)[:3])
    if (words == ['let', 'me'] or (not words and verb == 'believe' and (tail == 'me' or tail == 'it or not')) or
            (not words and verb == 'ask' and tail in ('anyone', 'anybody')) or
            (verb.endswith('ing') and words in (['been'], ['just', 'been'], ['stop'])) or
            (verb in NOUNS and 0 < len(words) <= 2 and words[-1] in OWN_NOUNS)):
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
    spots = spots_of(text, evidence)
    if not spots or len(spots) > MAX_SPOTS:
        return None
    source = Source(text)
    if not all(source.claims(at, evidence) for at in spots):
        return None
    # Read once more with each run of emoji as a space and each apostrophe inside a word as a plain one, the evidence
    # too (gene #190), and twice more with symbols, text faces, characters that show nothing and in-word emoji read
    # plainly (gene #198), and twice more as those, with what still hid a word in them read plainly first (gene #201),
    # and four times more with what's stacked read plainly first, two of them with a face before an aside dropped (gene
    # #207). Every reading must keep it, so each only ever refuses more.
    readings = {(plainly(text), plainly(evidence).strip())}
    readings |= {(plainer(said, invisible), plainer(note, invisible).strip()) for invisible in ('', ' ')
                 for said, note in ((text, evidence), (prepared(text), prepared(evidence)))}
    for invisible in ('', ' '):
        said, note = plainer(composed(text, invisible), invisible), plainer(composed(evidence, invisible), invisible)
        readings |= {(said, note.strip()), (asides_joined(said), asides_joined(note).strip())}
    for plain, said in readings - {(text, evidence)}:
        found, source = spots_of(plain, said) if said else [], Source(plain)
        if not found or len(found) > MAX_SPOTS or not all(source.claims(at, said) for at in found):
            return None
    # What a member is doing is only ever a projects note, and a projects note only ever says what they're doing.
    if not (DOING if candidate['category'] == 'projects' else LIKES).match(evidence):
        return None
    # Defense in depth; notes are still untrusted data even after validation.
    if re.search(r'(?i)\b(?:ignore|instructions?|system|developer|password|token|secret|address|diagnos\w*|'
                 r'politic\w*|religio\w*|sexual\w*|health|medical|permission|approv\w*|deploy|execute)\b', evidence):
        return None
    return candidate['category'], evidence


# An emoji is no word, break or quote of the member's: Unicode ones with their skin tones, joiners and variation
# selectors, keycaps with their digit, flags, and Discord's own `<:name:id>`. Inside a report or a condition one hid it
# ("i like chess, my mom 😂 would say", "if i win\nand finish 😂 early\n…"), as did an apostrophe spelled another way
# ("y‘all say") (gene #190).
EMOJI = (r"(?:<a?:\w{1,32}:\d{1,21}>|[0-9#*]\uFE0F?\u20E3|[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u27BF"
         r"\u2B00-\u2BFF\uFE0E\uFE0F\u200D\u20E3\U000E0020-\U000E007F])")
# A run of them with the spaces around it, matched only from the start of those spaces, so a long run of spaces is
# crossed once.
EMOJI_RUN = re.compile(rf"(?<![ \t])[ \t]*+{EMOJI}(?:[ \t]*+{EMOJI})*+[ \t]*+")
APOSTROPHE = re.compile(r"(?<=\w)[\u2018\u201B\u2032\uFF07\u00B4](?=\w)")


def plainly(text):
    """The text with each run of emoji, and the spaces around it, as one space, and each apostrophe inside a word as a
    plain one."""
    return APOSTROPHE.sub("'", EMOJI_RUN.sub(' ', text))


# Two more readings, which also only ever refuse more (gene #198). What #196's reading still missed: the older symbols
# members send as emoji (©, ™, ‼, ⁉, arrows) and text faces (":)", ";P", "xD", "<3"); characters that show nothing
# (zero-width spaces and joiners, word joiners, soft hyphens, direction marks, tags), read once as nothing ("don<ZWSP>t")
# and once as a space ("would<ZWSP>say"); apostrophes that are word characters ("yʼall"); and an emoji inside a word
# ("sa😂y", "ki😂dding"). No reading adds a comma or ends a condition: #199's over-refusals need their own design.
PICTURE = (r"[\U0001F000-\U0001FAFF\u00A9\u00AE\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA\u2300-\u23FF"
           r"\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2600-\u27BF\u2934\u2935\u2B00-\u2BFF\u3030\u303D\u3297"
           r"\u3299]")
# A face may run into the next word (":Pwhat", "<3who", "xDwho"), and takes its trailing brackets (":P)"); after a
# word only a plain one ("thanks:)"), so "5:30", "re:(x)" and "https://" stay what they are. Matched atomically: its
# alternatives overlap ("xD"), and nothing may try them again (Sol round 2 of #200).
FACE = (r"(?>(?<![\w:;=])[:;=][-'^]?(?:[)(\]\[/|*]+|[DPpOo3][)(\]\[/|*]*)|(?<=\w)[:;=][-'^]?(?:[)(]+|[DP])(?!\w)|"
        r"<3+|(?<![^\W\d_])[xX][Dd]+|[xX][Dd]+(?!\w)|\^_?\^|-_-|(?<!\w)T_T(?!\w))")
FACES = re.compile(FACE)
INWORD = rf"(?:<a?:\w{{1,32}}:\d{{1,21}}>|[0-9#*]\u20E3|{PICTURE})"  # What can stand inside a word; a face can't.
SYMBOL = rf"(?:{INWORD}|{FACE})"
SYMBOL_RUN = re.compile(rf"(?<![ \t])[ \t]*+{SYMBOL}(?:[ \t]*+{SYMBOL})*+[ \t]*+")
SHOWS_NOTHING = (r"[\u00AD\u034F\u061C\u115F\u1160\u17B4\u17B5\u180B-\u180F\u200B-\u200F\u202A-\u202E"
                 r"\u2060-\u206F\u3164\uFE00-\uFE0F\uFEFF\uFFA0\U0001D173-\U0001D17A\U000E0000-\U000E0FFF]")
INVISIBLE = re.compile(SHOWS_NOTHING)
APOSTROPHES = re.compile(r"(?<=\w)[\u2018\u201B\u2032\uFF07\u00B4\u02BC\u02BB](?=\w)")
# In a word that emoji or characters showing nothing split ("tol😂d😂Will", "would<ZWSP>sa😂y", "y😂’all"), each run of
# pieces that spells a word these rules look for is joined again; what stood between the others stays. Matched from a
# word's start only.
SEPARATOR = rf"(?:{INWORD}|{SHOWS_NOTHING})"
SPLIT = re.compile(rf"(?<![\w'’])\w[\w'’]*(?:{SEPARATOR}+[\w'’]+)+")
PIECES_OF = re.compile(rf"({SEPARATOR}+)")
LOOKED_FOR = frozenset(
    {word for rule in (FRAMED, REPORT, HEARD, NEGATED, ASKING, POLITE, RETRACTED, FILLER, COURTESY, TAKEN_BACK, CONDITION,
                       OPENS, ASIDE_PHRASES, RHETORICAL, ME_CLAUSE, DESCRIBED, BREAK, STARTS, PRIOR, IDIOM, LIKES, DOING,
                       INTRODUCES, POINTS_BACK, ASIDE_ONLY, TOLD)
     for word in re.findall(r"[a-z]{2,}", rule.pattern)}
    | ME | ME_BETWEEN | ADVERBS | NOUNS | PASSIVE | PARTICIPLES | ASIDE_WORDS | FORMS | OWN | UNFINISHED | CONTINUES
    | BACKREF | ACTIVITY | PLURALS | NAMES | set(ASKED) | {'u', 'i', 'thats', 'its'})
PIECES = max(map(len, LOOKED_FOR))  # Even a word split at every letter ("k😂i😂d😂d😂i😂n😂g").
# The member's own clause, past fillers, after which a run's text faces stay as typed (see plainer): one filler and what
# follows it at a time, so a message's run of them is crossed once, not once from each run of symbols in it (gene #201).
KEPT_FILLER = re.compile(r"(?i)(?:lol|lmao|haha|but|and|so|tho|though|ok|okay|well|honestly|anyway)\W+")
OWN_START = re.compile(r"(?i)(?:i|i'm|im|i've|ive|i'd|i'll|we|we're|we've|we'd|we'll)\b")


def past_fillers(text, at, filler, ends):
    """Where the fillers from `at` end; `ends` keeps each place already crossed, for the message's other runs."""
    crossed = []
    while at not in ends:
        step = filler.match(text, at)
        if not step:
            ends[at] = at
            break
        crossed.append(at)
        at = step.end()
    for place in crossed:
        ends[place] = ends[at]
    return ends[at]


@functools.lru_cache(maxsize=4096)
def looked_for(word):
    """Whether these rules look for this word: the words their patterns and word lists spell, and every form of the
    ones they match by pattern ("pretending", which a pattern spells as `pretend` and any ending, Sol round 2 of #200)."""
    word = word.lower().replace('’', "'")
    return (word in LOOKED_FOR or word.replace("'", '') in LOOKED_FOR
            or any(rule.fullmatch(word) for rule in (FRAMED, VERB, NEGATED, CONDITION, RETRACTED, TAKEN_BACK)))


def unsplit(m):
    pieces = PIECES_OF.split(m.group())  # Word, separator, word, …
    words, out, at = pieces[::2], [], 0
    while at < len(words):
        upto = next((end for end in range(min(len(words), at + PIECES), at + 1, -1)
                     if len(''.join(words[at:end])) <= PIECES and looked_for(''.join(words[at:end]))), at + 1)
        out.append(''.join(words[at:upto]))
        if upto < len(words):
            out.append(pieces[2 * upto - 1])  # What stood between this piece and the next stays.
        at = upto
    return ''.join(out)


@functools.lru_cache(maxsize=32)  # A message's readings serve each of its notes (gene #203).
def plainer(text, invisible):
    """gene #198's readings: apostrophes inside a word plain, a word split by emoji or characters showing nothing joined
    where it spells one these rules look for, other characters showing nothing as `invisible` ('' or ' '), each run of
    emoji, symbols and text faces with the spaces around it one space, and no run of spaces. Before the member's own
    clause, a run keeps its text faces as typed ("i like chess ;P 😂 i thought it'd be boring"): the ";" may end their
    sentence, and leaving it adds no refusal, where removing it would refuse what #196 keeps. The fillers are crossed
    once for all the message's runs, and a run without a face is a space whatever follows it (gene #201)."""
    text = INVISIBLE.sub(invisible, SPLIT.sub(unsplit, APOSTROPHES.sub("'", text)))
    runs, kept, ends = list(SYMBOL_RUN.finditer(text)), {}, {}
    for m in runs:
        if FACES.search(m.group()) and OWN_START.match(text, past_fillers(text, m.end(), KEPT_FILLER, ends)):
            kept[m.start()] = FACES.findall(m.group())
    out, last = [], 0
    for m in runs:
        faces = kept.get(m.start())
        out += [text[last:m.start()], f" {' '.join(faces)} " if faces else ' ']
        last = m.end()
    return re.sub(r'[ \t]{2,}', ' ', ''.join(out) + text[last:])


# What those readings still let hide a word (gene #201): a keycap's variation selector, which they read as showing
# nothing, so the keycap splits ("sa1️⃣y", "i1️⃣f"); an apostrophe beside an emoji or a character showing nothing,
# which they leave as typed ("don<ZWSP>ʼt", "y😂‘all"); and a face attached to a word, which they take only as
# ":)" or ":P" ("mom:P) would say", "mom:P] would say", "mom:] would say", "mom:p) would say", Sol round 1 of #205).
# The message is read twice more with each of these plain first: main's readings stay as they are.
SELECTED_KEYCAP = re.compile(r'(?<=[0-9#*])\uFE0F(?=\u20E3)')
NEAR = rf"(?:\w|{PICTURE}|{SHOWS_NOTHING}|[>\u20E3])"
STRAY = re.compile(rf"(?<={NEAR})[\u2018\u201B\u2032\uFF07\u00B4\u02BC\u02BB](?={NEAR}|<a?:)")
# Any face FACE knows, after a word: a letter of one and any trailing brackets, slashes, bars and stars, or only
# brackets, bars and stars, so "https://", "5:30" and "re:(x)" stay what they are.
ATTACHED = re.compile(r"(?<=\w)[:;=][-'^]?(?:[DPpOo3][)(\]\[/|*]*|[)(\]\[|*]+)(?!\w)")


@functools.lru_cache(maxsize=32)
def prepared(text):
    """The text with each keycap's variation selector gone, each apostrophe beside a word, an emoji or a character
    showing nothing plain, and each face attached to a word a space ("mom:P] would say" as "mom  would say")."""
    return ATTACHED.sub(' ', STRAY.sub("'", SELECTED_KEYCAP.sub('', text)))


# What those still let hide a word (gene #207): obfuscations stacked in one place. A character showing nothing inside a
# face or a keycap ("mom:<ZWSP>P] would say", "sa1<VS><ZWSP><keycap>y"), which they recognize before it's gone; a
# keycap's digit, which a split word's letters take, so the keycap splits nothing ("don1️⃣ʼ1️⃣t"); and a slash face
# attached to a word ("mom:/ would say"). And #201's item 4: a face kept before the member's own clause (see plainer)
# ends nothing when that clause is only an aside ("if i win ;P i guess", "my mom ;P i swear would say"). The message
# is read four times more, with these read plainly first: main's readings stay as they are.
SELECTORS = re.compile(r'[\U0000FE00-\U0000FE0F]')  # Never a space: they only pick how the character before shows.
KEYCAP = re.compile(r'[0-9#*]\U000020E3')
WHOLE_KEYCAP = '\U0001F51F'  # A keycap of its own, which no word's letters take (see SPLIT).
# A slash face after a word, never a link's "://" or a path's ":/x" (see ATTACHED).
SLASHED = re.compile(r"(?<=\w)[:;=]-?/(?![/\w])")
# The member's aside after a kept face: "i guess", "i swear", "we all know", "i mean".
OWN_ASIDE = re.compile(r"(?i)(?:i|we)\s+(?:(?:all|just|really|kinda|lowkey|honestly|also)\s+)?"
                       r"(?:guess|swear|mean|know|bet|suppose|think|figure|admit|promise)\b")
KEPT_FACES = re.compile(rf"(?<= )(?:{FACE} )+")


@functools.lru_cache(maxsize=32)
def composed(text, invisible):
    """The text with variation selectors gone, other characters showing nothing as `invisible` ('' or ' '), each
    keycap whole, then prepared, and each slash face attached to a word a space."""
    unseen = INVISIBLE.sub(invisible, SELECTORS.sub('', text))
    return SLASHED.sub(' ', prepared(KEYCAP.sub(WHOLE_KEYCAP, unseen)))


@functools.lru_cache(maxsize=32)
def asides_joined(text):
    """A reading (see plainer) with each run of faces it kept dropped where the member's clause after it, past fillers,
    is only an aside, and the aside too, as ASIDE_PHRASES blanks the member's others: what's around them reads as one
    clause ("if i win lol \\nplease call me dan", "my mom would say"). The fillers are crossed once for all the
    message's runs."""
    ends, out, last = {}, [], 0
    for m in KEPT_FACES.finditer(text):
        if m.start() < last:
            continue
        at = past_fillers(text, m.end(), KEPT_FILLER, ends)
        if aside := OWN_ASIDE.match(text, at):
            out += [text[last:m.start()], text[m.end():at]]
            last = aside.end()
    return ''.join(out) + text[last:]


def spots_of(text, evidence):
    """Where the evidence stands as whole words, stopping once there are more than MAX_SPOTS."""
    spots, at = [], text.find(evidence)
    while at >= 0 and len(spots) <= MAX_SPOTS:
        if whole(text, at, evidence):
            spots.append(at)
        at = text.find(evidence, at + 1)
    return spots


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
