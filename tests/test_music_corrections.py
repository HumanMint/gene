"""A member putting right the song Gene just played for them ("not that one, X") gets X queued, once."""
import asyncio
import datetime as dt
import gc
import time
from types import SimpleNamespace

import discord
import pytest

from gene.control_store import Controls
from gene.gene_controls import correction
from tests.test_gene_controls import control_body
from tests.test_runtime_controls import runtime

FIX = 'not the one, i meant join us for a bite'


@pytest.mark.parametrize('text,argument,expected', [
    # Review of #98/#157: the song is the start of the replacement, never words from a later clause, and a
    # change of heart after it withdraws it.
    ('wrong song, To The Moon, not To Be With You', 'To Be With You', False),
    ('wrong song, To The Moon, not To Be With You', 'To The Moon', True),
    ('i meant to skip To The Moon', 'To The Moon', False),
    ('wrong song, Love Story, actually no', 'Love Story', False),
    ('wrong song, Love Story... i changed my mind', 'Love Story', False),
    ('not that one, Love Story, no wait', 'Love Story', False),
    ('actually no, wrong song, Love Story', 'Love Story', True),
    ('not that one, Love Story by Taylor Swift', 'Love Story Taylor Swift', True),
    ('not that one, Love Story by Taylor Swift', 'Taylor Swift', False),
    ('wrong song, "Actually No" by someone', 'Actually No', True),
    (FIX, 'join us for a bite', True),
    ('fri not that one lol, Join Us For A Bite', 'join us for a bite', True),
    ('wrong song, "join us for a bite"', 'join us for a bite', True),  # Quoted song names are still theirs.
    ('i meant join us for a bite', 'join us for a bite', True),
    ('nah not that one \u2014 join us for a bite', 'join us for a bite', True),
    ('<@99> not that one, I Will Always Love You', 'I Will Always Love You', True),
    # Sol r2 S4: titles may use any words.
    ('wrong song, i meant Can You Feel My Heart', 'Can You Feel My Heart', True),
    ('wrong song, i meant All I Got by Said The Sky', 'All I Got by Said The Sky', True),
    ('wrong song, The One', 'The One', True),
    ('join us for a bite', 'join us for a bite', False),  # No correction: a plain title isn't a request.
    (FIX, 'someone else song', False),  # Not in their words.
    (FIX, '', False),
    ('for example: not that one, i meant join us for a bite', 'join us for a bite', False),
    ('"not that one, i meant join us for a bite" is what he said', 'join us for a bite', False),
    ("not that one, don't play join us for a bite", 'join us for a bite', False),
    ('join us for a bite, not that one', 'join us for a bite', False),  # The song comes after the correction.
    # Sol r1 and r2 S1: someone else's words in quotes, code or a blockquote never supply the song.
    ('not that one\n> alex: Never Gonna Give You Up', 'Never Gonna Give You Up', False),
    ('not that one\n```Never Gonna Give You Up```', 'Never Gonna Give You Up', False),
    ('not that one, ``Never Gonna Give You Up``', 'Never Gonna Give You Up', False),
    ('not that one\n>>> alex said\nNever Gonna Give You Up', 'Never Gonna Give You Up', False),
    ('"wrong song, Never Gonna Give You Up"\nnot that one, i meant Join Us For A Bite', 'Never Gonna Give You Up', False),
    # Sol r1 and r2 S2: chat that names no replacement, reported speech and questions aren't requests.
    ('i meant to say thanks', 'thanks', False),
    ('not that one, i meant to say thanks', 'thanks', False),
    ('not the one i wanted lol', 'the one', False),
    ('not the one i wanted lol', 'i wanted', False),
    ('he said not that one, i meant join us for a bite', 'join us for a bite', False),
    ('could you tell me if not that one, i meant join us for a bite works', 'join us for a bite', False),
    # gene #72 (1): the link is consumed once, and negation or cancellation outside a quoted title refuses.
    ("not that one, i meant don't play join us for a bite", 'join us for a bite', False),
    ('not that one, join us for a bite. do not queue anything yet', 'join us for a bite', False),
    ('wrong song, i meant join us for a bite, actually nvm', 'join us for a bite', False),
    ("wrong song, Don't Stop Believin'", "Don't Stop Believin'", False),
    ('wrong song, "Don\'t Stop Believin\'"', "Don't Stop Believin'", True),
    ("wrong song, I Don't Care", "I Don't Care", True),
    ('wrong song, Never Gonna Give You Up', 'Never Gonna Give You Up', True),
    # gene #72 (2): the song is the replacement right after the correction, never someone else's words or
    # an example, though a quoted title still counts.
    ('not that one, alex said "Never Gonna Give You Up"', 'Never Gonna Give You Up', False),
    ('not that one, for example Never Gonna Give You Up', 'Never Gonna Give You Up', False),
    ('not that one, alex said never gonna give you up', 'alex said never gonna give you up', False),
    ('not that one, e.g. never gonna give you up', 'e.g. never gonna give you up', False),
    ('not that one, join us for a bite. alex wants never gonna give you up', 'never gonna give you up', False),
    ('wrong song, join us for a bite, not never gonna give you up', 'never gonna give you up', False),
    ('wrong song, join us for a bite, not never gonna give you up', 'join us for a bite', True),
    ('wrong song, "Alex Said Hi"', 'Alex Said Hi', True),
    ('not that one, i said join us for a bite', 'join us for a bite', True),
    ('not that one, play That Funky Music', 'That Funky Music', True),
    ('not that one, play That Funky Music', 'Play That Funky Music', True),
    # gene #72 (3): only a conversational "i meant to say ..." is an infinitive; "To ..." can start a title.
    ('wrong song, To The Moon', 'To The Moon', True),
    ('not that one, i meant To The Moon', 'To The Moon', True),
    ('wrong song, i meant to say To The Moon', 'To The Moon', False),
    ('not that one, i meant to.', 'to', False),
    # gene #107 (1): after "i meant to", only a word that can't start an infinitive may start a title, whatever
    # the proposed song.
    ('i meant to skip To The Moon', 'to skip To The Moon', False),
    ('not that one, i meant to pause To The Moon', 'to pause To The Moon', False),
    ('wrong song, i meant to tag alex, To The Moon', 'to tag alex To The Moon', False),
    ('not that one, i meant To My Love', 'To My Love', True),
    ('not that one, i meant To Love Somebody', 'To Love Somebody', False),  # Fails closed; these two still count:
    ('wrong song, To Love Somebody', 'To Love Somebody', True),
    ("not that one, i meant 'To Love Somebody'", 'To Love Somebody', True),
    # gene #107 (2): more ways to take the replacement back.
    ('wrong song, Love Story, not that one either', 'Love Story', False),
    ("wrong song, Love Story, that's not it either", 'Love Story', False),
    ('wrong song, Love Story, nope', 'Love Story', False),
    ('wrong song, Love Story nope', 'Love Story', False),
    ('not that one, Love Story... nah', 'Love Story', False),
    ('wrong song, Love Story, no', 'Love Story', False),
    ('wrong song, "Not That One Either"', 'Not That One Either', True),
    ('wrong song, Hell No', 'Hell No', True),
    # gene #107 (3): connectors are skipped only after the title's first word.
    ('wrong song, From Me to You', 'Me to You', False),
    ('wrong song, From Me to You', 'From Me to You', True),
    ('not that one, By The Way', 'The Way', False),
    ('not that one, By The Way', 'By The Way', True),
    ('not that one, play From Me to You', 'Me to You', False),
    # gene #107 (4): single quotes, straight or curly, pair like double quotes; an apostrophe inside a word, or
    # leading one in "'til" or "'93", never opens or closes one.
    ('wrong song, \u2018Actually No\u2019', 'Actually No', True),
    ("wrong song, 'Actually No'", 'Actually No', True),
    ("wrong song, 'Not That One Either' by someone", 'Not That One Either', True),
    ("wrong song, 'Don't Stop Believin''", "Don't Stop Believin'", True),
    ('wrong song, \u2018Don\u2019t Stop Believin\u2019\u2019', "Don't Stop Believin'", True),
    ("wrong song, it's Love Story, don't play it", 'Love Story', False),
    ("not that one, 'Til I Collapse, don't play rockin' robin", "'Til I Collapse", False),
    ("not that one, '93 Til Infinity, don't play rockin' robin", '93 Til Infinity', False),
    ("wrong song, 'Til I Collapse", "'Til I Collapse", True),
    ("not that one, alex said 'Never Gonna Give You Up'", 'Never Gonna Give You Up', False),
    # gene #111 (1): only the title they quoted is protected, so a quoted refusal elsewhere still counts, and a
    # quote that might not close (a dropped letter with more words after it) protects nothing.
    ("wrong song, Love Story, 'don't play it' is what I said", 'Love Story', False),
    ('wrong song, Love Story, "don\'t play it" is what I said', 'Love Story', False),
    ("wrong song, 'Love Story, don't play rockin' robin", 'Love Story', False),
    ("wrong song, 'Love Story, don't play rockin' robin", "Love Story, don't play rockin'", False),
    ("wrong song, 'Love Story' 'nope'", 'Love Story', False),
    ("wrong song, 'Love Story, 'nope'", 'Love Story Nope', False),  # A second quote opening inside: ambiguous.
    ("wrong song, 'Love Story ' nope'", 'Love Story Nope', False),  # So is a loose mark.
    ("wrong song, 'Rockin' Robin'", "Rockin' Robin", True),
    ("wrong song, 'Rockin' Robin' by Bobby Day", "Rockin' Robin Bobby Day", True),
    ("wrong song, 'Don't Stop Believin'", "Don't Stop Believin'", True),
    ('wrong song, https://youtu.be/dont-nope', 'https://youtu.be/dont-nope', True),  # A link keeps every word too.
    ('wrong song, https://youtu.be/abc, nope', 'https://youtu.be/abc', False),
    ('wrong song, https://youtu.be/x', 'https://youtu.be', False),
    # gene #111 (2): a withdrawal word right after "by" names the artist.
    ('wrong song, Love Story by Nope', 'Love Story', True),
    ('wrong song, Love Story by Nope', 'Love Story by Nope', True),
    ('wrong song, Love Story by Nope', 'Love Story Nope', True),
    ('wrong song, Love Story by Taylor Swift, nope', 'Love Story Taylor Swift', False),
    ('wrong song, Love Story by Taylor Swift nope', 'Love Story', False),
    ('wrong song, Love Story by. no', 'Love Story by', False),  # "by." ends a sentence.
    ('not that one, ft. nope', 'ft nope', False),  # An artist follows a title, never starts one.
    # gene #111 (3): punctuation and closing filler don't hide a withdrawal.
    ('wrong song, Love Story, no thanks', 'Love Story', False),
    ('wrong song, Love Story, nah, lol', 'Love Story', False),
    ('wrong song, Love Story. no, lol', 'Love Story', False),
    ('not that one, Love Story... nah thanks', 'Love Story', False),
    ('wrong song, Love Story, actually nah', 'Love Story', False),
    ('wrong song, "Love Story" no', 'Love Story', False),
    ('wrong song, "No Thanks"', 'No Thanks', True),
    # gene #111 (4): a quoted title keeps every word, and only the connector before an artist may be skipped.
    ('wrong song, "Stand By Me"', 'Stand Me', False),
    ("wrong song, 'Stand By Me'", 'Stand Me', False),
    ('wrong song, Stand By Me', 'Stand Me', False),
    ('wrong song, Stand By Me', 'Stand By Me', True),
    ('wrong song, "Stand By Me" by Ben E. King', 'Stand By Me Ben E. King', True),
    ('wrong song, "Love Story" by Taylor Swift', 'Love Story Taylor Swift', True),
    ('wrong song, "Love Story by Taylor Swift"', 'Love Story Taylor Swift', False),
    ('wrong song, "The One"', 'One', False),
    # gene #111 (5): after the song, an example or someone else's words take it back; their own words don't.
    ("wrong song, 'Love Story' was an example", 'Love Story', False),
    ("wrong song, 'Love Story' is what I heard Alex say", 'Love Story', False),
    ('wrong song, Love Story was just an example', 'Love Story', False),
    ('wrong song, "Love Story", alex said', 'Love Story', False),
    ('wrong song, Love Story is what alex wants', 'Love Story', False),
    ('wrong song, Love Story according to alex', 'Love Story', False),
    ("wrong song, 'Love Story' is what i said", 'Love Story', True),
    ('wrong song, Love Story, like i said', 'Love Story', True),
    ("wrong song, Love Story, i've literally just said", 'Love Story', True),
    ('wrong song, Love Story by Example', 'Love Story', True),
    ('wrong song, Simon Says', 'Simon Says', True),
    # gene #119 (1): only an artist right after the title (or in it) is protected, and a quote is someone else's.
    ("wrong song, 'Love Story' was a quote from Nope", 'Love Story', False),
    ('wrong song, Love Story was a quote from Nope', 'Love Story', False),
    ("wrong song, 'Love Story' came from Nope", 'Love Story', False),
    ("wrong song, 'Love Story', quoting alex", 'Love Story', False),
    ('wrong song, Love Story is a quote from Taylor Swift', 'Love Story', False),
    ("wrong song, 'Love Story' by Nope", 'Love Story', True),
    # gene #119 (2): an artist's name may carry those words ("The Wanted"), though a withdrawal after its first word,
    # or someone else's words after it, still counts.
    ('wrong song, Glad You Came by The Wanted', 'Glad You Came', True),
    ("wrong song, 'Glad You Came' by The Wanted", 'Glad You Came', True),
    ('wrong song, Glad You Came by The Wanted', 'Glad You Came The Wanted', True),
    ('wrong song, All I Got by Said The Sky', 'All I Got', True),
    ('wrong song, Glad You Came by The Wanted nope', 'Glad You Came', False),
    ('wrong song, Glad You Came by The Wanted, alex said', 'Glad You Came', False),
    ('wrong song, Glad You Came by The Wanted is what alex said', 'Glad You Came', False),
    ('wrong song, Love Story by Taylor Swift alex said', 'Love Story', False),
    ('wrong song, Hell No by The Wanted', 'Hell No', True),
    ('wrong song, Love Story from the example', 'Love Story', False),
    # gene #119 (3): a title ends at its closing quote, or unquoted at a clause break, whatever the proposed song
    # covers...
    ("wrong song, 'Love Story' was an example", 'Love Story was an example', False),
    ("wrong song, 'Love Story' is what Alex said", 'Love Story is what Alex said', False),
    ("wrong song, 'Love Story' alex said", 'Love Story alex said', False),
    ("wrong song, 'Love Story' was a quote from Nope", 'Love Story was a quote from Nope', False),
    ('wrong song, Love Story was an example', 'Love Story was an example', False),
    ('wrong song, Love Story is what alex said', 'Love Story is what alex said', False),
    ('wrong song, Love Story, alex said', 'Love Story alex said', False),
    ('wrong song, Love Story like alex said', 'Love Story like alex said', False),
    ('wrong song, Love Story for example', 'Love Story for example', False),
    ('wrong song, Love Story by Taylor Swift is what alex wants', 'Love Story Taylor Swift is what alex wants', False),
    # ...though a title can still carry those words.
    ('wrong song, Say You, Say Me', 'Say You, Say Me', True),
    ('wrong song, Say, Say, Say', 'Say Say Say', True),
    ('wrong song, Everybody Wants to Rule the World', 'Everybody Wants to Rule the World', True),
    ('wrong song, This Is What You Came For', 'This Is What You Came For', True),
    ("wrong song, I Don't Want to Miss a Thing", "I Don't Want to Miss a Thing", True),
    ('wrong song, Please Please Please Let Me Get What I Want', 'Please Please Please Let Me Get What I Want', True),
    ('wrong song, Stand By Me by Ben E. King', 'Stand By Me Ben E. King', True),
    ('wrong song, Say You, Say Me by Nope', 'Say You, Say Me', True),
    ('wrong song, This Is What You Came For by Said The Sky', 'This Is What You Came For', True),
    # gene #119 (4): their own words don't vouch for a song they deny...
    ('wrong song, Love Story, I said not to play it', 'Love Story', False),
    ('wrong song, Love Story is not what I meant', 'Love Story', False),
    ('wrong song, Love Story is not what I meant', 'Love Story is not what I meant', False),
    ("wrong song, Love Story isn't what i asked for", 'Love Story', False),
    ("wrong song, 'Love Story' is not the one i wanted", 'Love Story', False),
    ('wrong song, Love Story by Taylor Swift is not what i meant', 'Love Story', False),
    ("wrong song, Love Story is what i didn't ask for", 'Love Story', False),
    ('wrong song, Love Story, i said no', 'Love Story', False),
    ('wrong song, Love Story, i told you not to', 'Love Story', False),
    ('wrong song, Say You, Say Me by The Wanted is not what i meant', 'Say You, Say Me', False),
    # gene #126 round 1: a hedged denial, or a refusal with its object, still denies it.
    ('wrong song, Glad You Came by The Wanted is absolutely not what I meant', 'Glad You Came', False),
    ('wrong song, Love Story is absolutely not what I meant', 'Love Story', False),
    ('wrong song, Love Story is just not the one i wanted', 'Love Story', False),
    ("wrong song, Love Story isn't really what i asked for", 'Love Story', False),
    ('wrong song, Glad You Came by The Wanted, I said no to that', 'Glad You Came', False),
    ('wrong song, Love Story, I said no to that', 'Love Story', False),
    ('wrong song, Love Story, i said not to do that', 'Love Story', False),
    ('wrong song, Love Story, I said not to ever play it', 'Love Story', False),
    ('wrong song, Love Story is what they want', 'Love Story', False),
    # ...while they still count, and a complaint about the song Gene played isn't a denial.
    ('wrong song, Love Story is what i meant', 'Love Story', True),
    ('wrong song, Love Story, i said it already', 'Love Story', True),
    ('wrong song, Love Story, not Glad You Came like i said', 'Love Story', True),
    ("wrong song, Love Story, that's not what i asked for", 'Love Story', True),
    # A bare "say" or "want" in the song they named is its wording, not someone else's words.
    ('wrong song, I Like the Way You Say My Name', 'I Like the Way You Say My Name', True),
    ('wrong song, This Is What You Want', 'This Is What You Want', True),
    ('wrong song, Love Story like alex wanted', 'Love Story like alex wanted', False),
    ('wrong song, Love Story is what they want', 'Love Story is what they want', False),
    # gene #126 round 2: "like you say" and "what we want" are someone else's words or a denial, and a withdrawal
    # can end with "please" or "again".
    ('wrong song, Love Story, like you say', 'Love Story like you say', False),
    ('wrong song, Love Story as you say', 'Love Story as you say', False),
    ('wrong song, Love Story is not what we want', 'Love Story is not what we want', False),
    ('wrong song, Love Story is not what we want', 'Love Story', False),
    ('wrong song, Love Story, i said no to that one please', 'Love Story', False),
    ('wrong song, Love Story, i said not to do it again', 'Love Story', False),
    ('wrong song, We Want the Airwaves', 'We Want the Airwaves', True),
    ('wrong song, Say No to This', 'Say No to This', True),
    ('wrong song, Love Story again please', 'Love Story', True),
    # gene #128 (1): a contracted denial ends an unquoted title like "is not" does, whatever the proposed song
    # covers, though a quoted title, or one with a contraction that denies nothing, still counts.
    ("wrong song, Love Story isn't what i asked for", "Love Story isn't what i asked for", False),
    ("wrong song, Love Story wasn't what i meant", "Love Story wasn't what i meant", False),
    ("wrong song, Love Story ain't what i asked for", "Love Story ain't what i asked for", False),
    ('wrong song, Love Story isnt the one i wanted', 'Love Story isnt the one i wanted', False),
    ("wrong song, Love Story isn't what alex wants", "Love Story isn't what alex wants", False),
    ("wrong song, 'Love Story isn't what i asked for'", "Love Story isn't what i asked for", True),
    ("wrong song, It Ain't Me", "It Ain't Me", True),
    ("wrong song, Love Isn't Easy", "Love Isn't Easy", True),
    # gene #128 (2): a closing withdrawal may end "this time" or "ever again".
    ('wrong song, Love Story, i said no to that one this time', 'Love Story', False),
    ('wrong song, Love Story, i said not to do it ever again', 'Love Story', False),
    ('wrong song, Love Story, i said no this time lol', 'Love Story', False),
    ('wrong song, Love Story... nah this time', 'Love Story', False),
    ("wrong song, 'I Said No to That One This Time'", 'I Said No to That One This Time', True),
    ('wrong song, Love Story this time', 'Love Story', True),
    ('wrong song, Love Story, i said it this time', 'Love Story', True),
    # gene #128 (3): an inclusive "we" is the speaker's own, though a collective denial or withdrawal still counts,
    # and so does someone else's words after it.
    ('wrong song, Love Story is what we want', 'Love Story', True),
    ('wrong song, Love Story is what we wanted', 'Love Story', True),
    ('wrong song, Love Story is what we meant', 'Love Story', True),
    ("wrong song, Love Story isn't what we wanted", 'Love Story', False),
    ("wrong song, Love Story is what we didn't ask for", 'Love Story', False),
    ('wrong song, Love Story is what we never wanted', 'Love Story', False),
    ("wrong song, Love Story is what we don't want", 'Love Story', False),
    ('wrong song, Love Story by Taylor Swift we said no to that', 'Love Story', False),
    ('wrong song, Love Story by Taylor Swift we told you not to', 'Love Story', False),
    ('wrong song, Love Story is what we heard alex say', 'Love Story', False),
    ('wrong song, Love Story is what we were told', 'Love Story', False),
    ('wrong song, Love Story is what we said alex wanted', 'Love Story', False),
    ('wrong song, Love Story, we said it already', 'Love Story', True),
    ('wrong song, Love Story, we said no to that', 'Love Story', False),
    ('wrong song, Love Story, we said not to play it', 'Love Story', False),
    ('not that one, we said Love Story', 'Love Story', False),  # Only "i said" may lead the song.
    # gene #133 (1): "no more" and "not now" take the song back after "i said" or "we said", like "no" and "not".
    ('wrong song, Love Story, we said no more', 'Love Story', False),
    ('wrong song, Love Story, we said not now', 'Love Story', False),
    ('wrong song, Love Story, i said no more', 'Love Story', False),
    ('wrong song, Love Story, i said not now', 'Love Story', False),
    ('wrong song, Love Story, we said not right now lol', 'Love Story', False),
    ('wrong song, Love Story, i told you not anymore', 'Love Story', False),
    ('wrong song, Love Story, we said no more of that please', 'Love Story', False),
    ('wrong song, Love Story, i said not rn', 'Love Story', False),
    ('wrong song, Love Story, we said not for now', 'Love Story', False),
    ('wrong song, Love Story, i said no more of those songs', 'Love Story', False),
    ('wrong song, Love Story, i said now', 'Love Story', True),
    ('wrong song, Love Story, we said no more Shake It Off', 'Love Story', True),
    ("wrong song, 'We Said No More'", 'We Said No More', True),
    # So do a closing "not now" or "no more" after a pause or a "no", like a closing "no".
    ('wrong song, Love Story, not now', 'Love Story', False),
    ('wrong song, Love Story. no more lol', 'Love Story', False),
    ('wrong song, Love Story nah not now', 'Love Story', False),
    ('wrong song, Love Story, no, not right now', 'Love Story', False),
    ('wrong song, Love Story, i said no, not now', 'Love Story', False),
    ('wrong song, Love Story, No More Drama', 'Love Story', True),
    ('wrong song, If Not Now', 'If Not Now', True),
    ('wrong song, No More', 'No More', True),
    # A title that is itself a "not now" or "no more" is the title, after "i said" as anywhere (Sol r1 on #151).
    ('not that one, i said Not Today', 'Not Today', True),
    ("not that one, i said 'Not Today'", 'Not Today', True),
    ('not that one, i meant Not Today', 'Not Today', True),
    # gene #133 (2): their own denial with someone else alongside ("alex and i") is still theirs.
    ("wrong song, Love Story isn't what alex and i asked for", 'Love Story', False),
    ('wrong song, Love Story is not what you and i wanted', 'Love Story', False),
    ("wrong song, Love Story is what alex and i didn't ask for", 'Love Story', False),
    ("wrong song, Love Story isn't the one alex, sam and i picked", 'Love Story', False),
    ('wrong song, Love Story is not what alex & i had in mind', 'Love Story', False),
    ("wrong song, Love Story by Taylor Swift isn't what alex and i asked for", 'Love Story', False),
    ("wrong song, Love Story isn't what me and alex asked for", 'Love Story', False),
    ('wrong song, Love Story is what alex and i asked for', 'Love Story', True),
    ("wrong song, 'Love Story Isn't What You and I Wanted'", "Love Story Isn't What You and I Wanted", True),
    # gene #133 (3): an artist's name is no one's words, after a quoted title as after a plain one.
    ("wrong song, 'All I Got' by Said The Sky", 'All I Got', True),
    ('wrong song, "Love" by Said The Sky', 'Love', True),
    ('wrong song, Love by Said The Sky', 'Love', True),
    ('wrong song, All I Got by Said The Sky', 'All I Got', True),
    ("wrong song, 'Love' by Mentioned Band", 'Love', True),
    ("wrong song, 'All I Got' by Said The Sky, alex said so", 'All I Got', False),
    ("wrong song, 'All I Got', alex said so", 'All I Got', False),
    ("wrong song, 'Love' by Alex said Taylor", 'Love', False),
    # Masking it keeps its words: their own words after a two-word artist stay out of reach, as on main.
    ("wrong song, 'It Wasn't Me' by The Wanted i suggested lol", "It Wasn't Me", True),
    ("wrong song, 'It Wasn't Me' by The Wanted we mentioned lol", "It Wasn't Me", True),
    # Sol r1 on #151: a closing "not now" or "no more" is read only after the song they named. Inside it, it's the
    # title, as on main, and so is the song said again; another one after it still takes it back.
    ('wrong song, No More, No More', 'No More No More', True),
    ('wrong song, No, Not Now', 'No Not Now', True),
    ('wrong song, We Said No More', 'We Said No More', True),
    ('wrong song, No More, No More', 'No More', True),
    ('wrong song, Not Now, i said not now', 'Not Now', True),
    ('wrong song, No More, not now', 'No More', False),
    ('wrong song, Not Now, no more lol', 'Not Now', False),
    ('wrong song, No, not now', 'No', False),
    # Sol r2 on #151: the song said again is its title, without an artist they named after it.
    ("wrong song, 'Not Now' by Blink-182, i said 'Not Now'", 'Not Now Blink-182', True),
    ('wrong song, Not Now by Blink-182, i said not now', 'Not Now Blink-182', True),
    ('wrong song, Not Now by Blink-182, not now', 'Not Now Blink-182', True),
    ('wrong song, Love Story by Taylor Swift, not now', 'Love Story Taylor Swift', False),
    ("wrong song, 'No More' by Blink-182, not now", 'No More Blink-182', False),
    # Their own subject first, with someone after it ("i and alex", "me and the boys"), and "no more tracks".
    ("wrong song, Love Story isn't what i and alex chose", 'Love Story', False),
    ("wrong song, Love Story isn't what me and the boys picked", 'Love Story', False),
    ("wrong song, Love Story is what i and alex didn't ask for", 'Love Story', False),
    ("wrong song, 'Love Story Isn't What I and Alex Chose'", "Love Story Isn't What I and Alex Chose", True),
    ('wrong song, Love Story, we said no more tracks', 'Love Story', False),
    # gene #152 (1): an artist they named runs to a clause break, another artist, their own "i" or "we", or the end,
    # so its second word is its name, not someone's words ("by She Wants Revenge"), quoted or not...
    ("wrong song, 'Tear You Apart' by She Wants Revenge", 'Tear You Apart', True),
    ('wrong song, Tear You Apart by She Wants Revenge', 'Tear You Apart', True),
    ('wrong song, "Tear You Apart" by "She Wants Revenge"', 'Tear You Apart', True),
    ("wrong song, 'Tear You Apart' by 'She Wants Revenge'", 'Tear You Apart', True),
    ('wrong song, Say You, Say Me by She Wants Revenge', 'Say You, Say Me She Wants Revenge', True),
    ("wrong song, 'Tear You Apart' by She Wants Revenge, i said it already", 'Tear You Apart', True),
    ('wrong song, "Love" by "Alex Said Taylor"', 'Love', True),
    # A quoted artist is masked word by word, its opening mark with its first word, so their own words after it read
    # as on main.
    ("wrong song, 'Love' by 'She Wants Revenge' we said it already", 'Love', True),
    # ...though a withdrawal or someone else's words after it still count, and so does a verb that isn't its second
    # word, has no name after it, or introduces someone's words ("'Love' by Alex said Taylor").
    ("wrong song, 'Tear You Apart' by She Wants Revenge nope", 'Tear You Apart', False),
    ("wrong song, 'Tear You Apart' by She Wants Revenge, alex said", 'Tear You Apart', False),
    ("wrong song, 'Tear You Apart' by She Wants Revenge is what alex wants", 'Tear You Apart', False),
    ('wrong song, \'Tear You Apart\' by "She Wants Revenge" alex said', 'Tear You Apart', False),
    ('wrong song, \'Tear You Apart\' by "She Wants Revenge", not now', 'Tear You Apart', False),
    ('wrong song, Love Story by Taylor wants', 'Love Story', False),
    ('wrong song, Love Story by Taylor wants it', 'Love Story', False),
    ('wrong song, Love Story by Taylor wants they chose', 'Love Story', False),
    ("wrong song, 'Love' by Alex asked for", 'Love', False),
    ('wrong song, Love Story by Taylor alex wants Despacito', 'Love Story', False),
    ('wrong song, Love Story by Alex said Taylor', 'Love Story', False),
    ('wrong song, Stand By Me Wants Revenge', 'Stand By Me', False),  # "By Me" names no one.
    # A denial reads across a quoted artist as across a plain one.
    ('wrong song, \'Love\' by "Taylor" isn\'t what we chose', 'Love', False),
    ('wrong song, Say You, Say Me by "She Wants Revenge" isn\'t what we chose', 'Say You, Say Me', False),
    # gene #152 (2): their own closing "i said not now" or "we said no more" takes the song back even when the song
    # they proposed took it in, unless it's that song said again, and a title they quoted keeps its words.
    ('wrong song, Love Story, we said no more', 'Love Story we said no more', False),
    ('wrong song, Love Story, i said not now', 'Love Story i said not now', False),
    ('wrong song, Love Story, we said not right now lol', 'Love Story, we said not right now lol', False),
    ('wrong song, Love Story, i said no, not now', 'Love Story i said no not now', False),
    ('wrong song, Love Story by Taylor Swift, i told you not anymore', 'Love Story Taylor Swift i told you not anymore',
     False),
    ('wrong song, Not Now, i said not now', 'Not Now i said not now', True),
    ('wrong song, Not Now by Blink-182, i said not now', 'Not Now Blink-182 i said not now', True),
    ('wrong song, "Love Story, we said no more"', 'Love Story we said no more', True),
    # gene #152 (3): the song said again is the title they quoted without an artist they quoted with it...
    ('wrong song, "Not Now by Blink-182", i said "Not Now"', 'Not Now by Blink-182', True),
    ('wrong song, "Not Now from Blink-182", i said "Not Now"', 'Not Now from Blink-182', True),
    ('wrong song, "Not Now feat. Blink-182", i said "Not Now"', 'Not Now feat. Blink-182', True),
    ('wrong song, "No More by Blink-182", not now', 'No More by Blink-182', False),
    ("wrong song, 'Not Now By Me', i said not now", 'Not Now By Me', False),  # "By Me" names no one.
    # gene #152 (4): ...and may leave out a leading article, as naming the song may.
    ('wrong song, the Not Now by Blink-182, i said "Not Now"', 'Not Now Blink-182', True),
    ('wrong song, the Not Now by Blink-182, not now', 'Not Now Blink-182', True),
    ('wrong song, the Not Now by Blink-182, i said "Not Now"', 'The Not Now Blink-182', True),
    ('wrong song, the No More by Blink-182, not now', 'No More Blink-182', False),
    # gene #156 (1): past an artist they named, the title is over, so a closing "not now" or "no more" after a pause
    # takes the song back even when the song they proposed took it in, quoted artist or plain...
    ('wrong song, Glad You Came by "The Wanted", not now', 'Glad You Came The Wanted not now', False),
    ('wrong song, Glad You Came by "The Wanted" not now', 'Glad You Came The Wanted not now', False),
    ('wrong song, Tear You Apart by "She Wants Revenge", not now', 'Tear You Apart She Wants Revenge not now', False),
    ("wrong song, Glad You Came by 'The Wanted', no more", 'Glad You Came The Wanted no more', False),
    ('wrong song, Love Story by Taylor Swift, not now', 'Love Story Taylor Swift not now', False),
    ('wrong song, Love Story by Taylor Swift nah not now lol', 'Love Story Taylor Swift nah not now lol', False),
    # ...though not the song said again, a title they quoted, or after a connector that names no one ("By Me").
    ('wrong song, Not Now by "Blink-182", not now', 'Not Now Blink-182 not now', True),
    ('wrong song, the Not Now by "Blink-182", not now', 'Not Now Blink-182 not now', True),
    ('wrong song, "Love Story by Taylor Swift, not now"', 'Love Story by Taylor Swift not now', True),
    ('wrong song, Stand By Me, No More', 'Stand By Me No More', True),
    ('wrong song, Stand By "Me", No More', 'Stand By Me No More', True),
    ('wrong song, Stand By ‘Me’, No More', 'Stand By Me No More', True),
    # gene #156 (2): a verb reporting someone's words, or hearing them, is never an artist's name, as one introducing
    # them isn't ("by Alex said Taylor"); a verb handing the song to someone may still be its second word.
    ("wrong song, 'Love Story' by Taylor told Alex", 'Love Story', False),
    ("wrong song, 'Love Story' by Taylor asked Alex", 'Love Story', False),
    ('wrong song, Love Story by Taylor tells Alex', 'Love Story', False),
    ("wrong song, 'Love Story' by Taylor asks Alex", 'Love Story', False),
    ("wrong song, 'Love Story' by Taylor say Alex", 'Love Story', False),
    ("wrong song, 'Love Story' by Taylor mentions Alex", 'Love Story', False),
    ("wrong song, 'Love Story' by Taylor heard Alex", 'Love Story', False),
    ("wrong song, 'Love' by Alex Picked Taylor", 'Love', True),
    ('wrong song, "Love" by "Taylor Told Alex"', 'Love', True),  # What they quoted is the artist's, every word.
    # gene #156 (3): a pronoun they quoted after a connector names no one either, whichever quote they used.
    ('wrong song, "Not Now by ‘Me’", not now', 'Not Now by Me', False),
    ('wrong song, “Not Now by ‘Me’”, not now', 'Not Now by Me', False),
    ('wrong song, "Not Now by ‘Me’", i said not now', 'Not Now by Me', False),
    ('wrong song, "Not Now by \'Me\'", not now', 'Not Now by Me', False),
    ('wrong song, "Not Now by ‘Blink-182’", i said "Not Now"', 'Not Now by Blink-182', True),
    # A name of several words they quoted is a name, even one starting with a pronoun (round 1 of #165's review).
    ('wrong song, "Not Now by ‘My Chemical Romance’", i said "Not Now"', 'Not Now by My Chemical Romance', True),
    ('wrong song, "Not Now by “My Chemical Romance”", i said "Not Now"', 'Not Now by My Chemical Romance', True),
    ('wrong song, "Not Now by \'Our Lady Peace\'", i said "Not Now"', 'Not Now by Our Lady Peace', True),
    ('wrong song, Glad You Came by "My Chemical Romance", not now', 'Glad You Came My Chemical Romance not now', False),
])
def test_correction_needs_the_words_and_the_song_in_their_own_message(text, argument, expected):
    assert correction(text, argument) is expected


# Worst cases for each check: a head, a unit repeated to the length wanted, and a tail.
SHAPES = [("'", "a' ", ''), ("'", "rockin' ", ''), ('', "''a ", ''), ('', "' ", ''), ('"', 'a ', ''),
          ('love story nah', ' lol', ' x'), ('love story', ', no lol', ' x'), ('love story', ' ,', 'x'),
          ('love story', ', a b said', ''), ('love ', 'by ', ''), ('love', ' from x', ''), ('love story', ' i just', ' said'),
          ('love story ', "don't ", ''), ('i meant ', 'to ', ''), ('love story', ' is not what', ''), ('love ', 'by the ', ''),
          ('love story', ' i said no', ''), ('love story', ', say', ''), ('love story ', 'x ', 'is not what i meant'),
          ('love story', ' is absolutely', ''), ('love story', ', i said no to that', ''), ('love story', ' not to ever', ''),
          ('i like ', 'you say ', ''), ('love story', " isn't what", ''), ('love story, i said no', ' this time', ' x'),
          ('love story', ' we just', ' said'), ('love story', ', we said no to that', ''),
          ('love story, i said not to', ' ever again', ' x'), ('love story', ', we said no more', ''),
          ('love story, i said not right now', ' lol', ' x'), ('love story', " isn't what alex and", ' i said'),
          ("'all i got'", ' by said the sky', ''), ('love story', ', not now', ' x'), ('love story', ' nah not now', ' x'),
          ('love story', ', no more of those songs', ''), ('love story', ' is what x, y and', ' i never asked'),
          ('love story', " isn't what i and", ' x chose'), ('no more', ', no more', ' x'),
          ('love story', ' by she wants revenge', ''), ('love', ' by "she wants revenge"', ''), ('love', ' by a b c d', ''),
          ('love', " by 'a", ''), ('love story', ', i said no, not now', ' x'), ('love', " by 'a'", ''),
          ('love', ' by "a"', ', not now'), ('love', ' by \u2018me\u2019', ', not now'), ('love', ' by a told b', ''),
          ('"love', ' by \u2018me\u2019', '", not now')]


def message(shape, length=4000):
    """The shape as a correction of about `length` characters, its head and tail whole."""
    head, unit, tail = shape
    return 'wrong song, '+head+unit*((length-12-len(head)-len(tail))//len(unit))+tail


def read(text):
    """Every check, for the arguments a correction might propose, up to the whole message: with every artist in the
    song they named, a check made once per artist reads them all."""
    for argument in ('love story', 'a', text[12:200], text[12:]):
        correction(text, argument)


def test_a_long_message_is_read_without_backtracking():
    """Each check runs in about linear time, so a 4000-character worst case takes milliseconds, not minutes."""
    read(message(SHAPES[0]))  # Warmed up first: a slow CI runner's first call isn't backtracking.
    for shape in SHAPES:
        began = time.perf_counter()
        read(message(shape))
        # Milliseconds here; backtracking would take seconds to minutes, so the bound has room for a busy runner.
        assert time.perf_counter()-began < 2, shape


def fastest_reads(texts, runs=5):
    """Each text's fastest read in CPU time, taking turns so a slow spell on the runner hits them alike."""
    best = [float('inf')]*len(texts)
    for _ in range(runs):
        for i, text in enumerate(texts):
            began = time.thread_time()  # Not the runner's other work.
            read(text)
            best[i] = min(best[i], time.thread_time()-began)
    return best


def test_reading_time_grows_with_the_length_not_its_square():
    """The ceiling above only catches backtracking slow enough to cross it (gene #140). Four times the characters
    takes about four times as long when every check reads in linear time, and about sixteen when one backtracks
    quadratically, however fast it still is, so the ratio of each shape's 4000-character read to its 1000 must stay
    under 8."""
    gc.collect()
    gc.disable()  # A collection during a read is noise, not reading time.
    try:
        for shape in SHAPES:
            texts = [message(shape, length) for length in (1000, 4000)]
            fastest_reads(texts, runs=1)  # Warmed up first.
            ratios = []
            while len(ratios) < 3 and (not ratios or ratios[-1] >= 8):  # Backtracking is slow on every try; noise, rarely.
                small, large = fastest_reads(texts)
                ratios.append(large/small)
            assert ratios[-1] < 8, (shape, ratios)
    finally:
        gc.enable()


def played(store, **changes):
    """The member's earlier play, finished by Gene a minute ago."""
    controls = Controls(store)
    first = control_body(**{'source': {**control_body()['source'], 'id': str(discord.utils.time_snowflake(
        dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)))}, **changes})
    controls.enqueue(first)
    claim = controls.poll(['play'])
    controls.finish(claim['id'], claim['lease'], {'output': 'Track Enqueued'})
    with store.connection() as db:
        db.execute('UPDATE gene_controls SET created=created-60')
    return claim['id']


def fix(**changes):
    return control_body(**{'request_text': FIX, 'argument': 'join us for a bite', **changes})


def test_a_correction_after_their_own_play_is_queued_once(store):
    prior = played(store)
    controls = Controls(store)
    controls.enqueue(fix())
    claim = controls.poll(['play'])
    assert claim['argument'] == 'join us for a bite' and claim['correction_of'] == prior
    with store.connection() as db:
        db.execute('UPDATE gene_controls SET created=created-10')
    # One correction per play, even from a fresh message. (Once Gene finishes this one, it's the latest play,
    # and "not that one either" can put it right in turn.)
    with pytest.raises(ValueError):
        controls.enqueue(fix(source={**control_body()['source'], 'id': str(int(claim['source']['id'])+(1 << 22))}))


@pytest.mark.parametrize('change', ['member', 'conversation', 'old', 'unfinished', 'not play', 'words', 'none'])
def test_a_correction_needs_their_own_recent_finished_play_here(store, change):
    if change == 'member':
        played(store, user_id='21', source={**control_body()['source'], 'user_id': '21',
               'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)))})
    elif change == 'conversation':
        played(store, conversation_id='public:2', channel_id='2', target_channel_id='2',
               source={**control_body()['source'], 'channel_id': '2',
                       'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)))})
    elif change == 'not play':
        played(store, action='skip', argument='', request_text='fri skip the song')
    elif change != 'none':
        played(store)
    with store.connection() as db:
        if change == 'old':
            db.execute('UPDATE gene_controls SET created=created-600')
        elif change == 'unfinished':
            db.execute("UPDATE gene_controls SET state='claimed'")
    body = fix(argument='someone else song') if change == 'words' else fix()
    with pytest.raises(ValueError):
        Controls(store).enqueue(body)


def test_gene_runs_a_correction_the_controller_found_and_nothing_else_without_a_request():
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, message = await runtime()
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        message.content = FIX
        try:
            plain = {**claim, 'request_text': FIX, 'argument': 'join us for a bite'}
            assert 'blocked' in await runner.execute(cog, plain)
            assert not calls
            result = await runner.execute(cog, {**plain, 'correction_of': 'earlier'})
            assert 'Track Enqueued' in result['output'] and len(calls) == 1
        finally:
            await bot.close()
    asyncio.run(scenario())


def test_fri_hears_whether_a_correction_would_find_a_play_even_past_three_reads(store, monkeypatch):
    """Sol r1 S3: the three receipts can all be reads, so the controller says whether one would count."""
    controls = Controls(store)
    assert not controls.correctable('20', '123', 'public:1')
    played(store)
    for action, text in (('queue', 'fri show the queue'), ('now_playing', "fri what's playing"), ('status', 'fri gene status')):
        with store.connection() as db:
            db.execute('UPDATE gene_controls SET created=created-5')
        controls.enqueue(control_body(**{'action': action, 'argument': '', 'request_text': text,
            'source': {**control_body()['source'], 'id': str(discord.utils.time_snowflake(
                dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=30-len(action))))}}))
        claim = controls.poll([action])
        controls.finish(claim['id'], claim['lease'], {'output': 'ok'})
    assert [r['action'] for r in controls.recent('20', '123', 'public:1')] == ['status', 'now_playing', 'queue']
    assert controls.correctable('20', '123', 'public:1')
    assert not controls.correctable('20', '123', 'public:2')
