from util.event import Event
from util.types import BotLike
from util import Mapping, commandSplit, TimeoutException
from util.irctools import escape_control_codes as esc, AAA
from twisted.words.protocols.irc import CHANNEL_PREFIXES
from random import randint


WAIT_TIMEOUT = 5
PRIVMSG_WAIT_TIMEOUT = 2

# https://tools.ietf.org/html/rfc1459#section-4.2.1
JOIN_ERRORS = {
    "ERR_NOSUCHCHANNEL",
    "ERR_TOOMANYCHANNELS",
    "ERR_NEEDMOREPARAMS",
    "ERR_CHANNELISFULL",
    "ERR_INVITEONLYCHAN",
    "ERR_BANNEDFROMCHAN",
    "ERR_BADCHANNELKEY",
    "ERR_BADCHANMASK",
}

# https://tools.ietf.org/html/rfc1459#section-4.2.2
PART_ERRORS = {"ERR_NEEDMOREPARAMS", "ERR_NOSUCHCHANNEL", "ERR_NOTONCHANNEL"}

# https://tools.ietf.org/html/rfc1459#section-4.2.8
KICK_ERRORS = {
    "ERR_NEEDMOREPARAMS",
    "ERR_NOSUCHCHANNEL",
    "ERR_BADCHANMASK",
    "ERR_CHANOPRIVSNEEDED",
    "ERR_NOTONCHANNEL",
}

# https://tools.ietf.org/html/rfc1459#section-4.4.1
# RPL_AWAY ignored because I don't think we'll ever care, but it might be
# desirable for other people
PRIVMSG_ERRORS = {
    "ERR_NORECIPIENT",
    "ERR_NOTEXTTOSEND",
    "ERR_CANNOTSENDTOCHAN",
    "ERR_NOTOPLEVEL",
    "ERR_WILDTOPLEVEL",
    "ERR_TOOMANYTARGETS",
    "ERR_NOSUCHNICK",
}


def admin_join(event: Event, bot: BotLike) -> None:
    channel, key = commandSplit(event.argument)
    if not channel:
        return bot.say(".%s #channel [key]" % event.command)
    elif channel[0] not in CHANNEL_PREFIXES:
        channel = "#" + channel

    try:
        for w_event in bot.send_and_wait(
            None,
            stope=("joined", *JOIN_ERRORS),
            f=bot.join,
            fargs=(channel, key),
            timeout=WAIT_TIMEOUT,
        ):
            if w_event.type == "joined" and w_event.target == channel:
                r_msg = "Successfully joined (%s)" % channel
                if key:
                    r_msg += " with key (%s)" % key
                bot.say(r_msg + ".")
                break
            elif w_event.type in JOIN_ERRORS:
                if w_event.params[1] != channel:
                    continue
                r_msg = "Failed to join (%s): %s" % (channel, w_event.type)
                if w_event.params[2]:
                    r_msg += " - %s" % w_event.params[2]
                bot.say(r_msg)
                break
    except TimeoutException:
        bot.say("Failed to join (%s) in (%d second) timeout." % (channel, WAIT_TIMEOUT))


def admin_part(event: Event, bot: BotLike) -> None:
    channel, reason = commandSplit(event.argument)

    if not channel:
        bot.say("Bye.")
        channel = event.target
    elif channel[0] not in CHANNEL_PREFIXES:
        channel = "#" + channel

    try:
        for w_event in bot.send_and_wait(
            None,
            stope=("left", *PART_ERRORS),
            f=bot.leave,
            fargs=(channel, reason),
            timeout=WAIT_TIMEOUT,
        ):
            if w_event.type == "left" and w_event.target == channel:
                # We already said bye in this case
                if channel != event.target:
                    r_msg = "Successfully parted (%s)" % channel
                    if reason:
                        r_msg += " with reason (%s)" % reason
                    bot.say(r_msg + ".")
                break
            r_msg = "Failed to part from (%s): %s" % (channel, w_event.type)
            if w_event.params[2]:
                r_msg += " - %s" % w_event.params[2]
            bot.say(r_msg)
            break
    except TimeoutException:
        bot.say("Failed to part (%s) in (%d second) timeout." % (channel, WAIT_TIMEOUT))


def admin_kick(event: Event, bot: BotLike) -> None:
    channel, user = commandSplit(event.argument)
    user, reason = commandSplit(user)

    if not channel or not user:
        return bot.say(".%s #channel user [reason]" % event.command)
    elif channel[0] not in CHANNEL_PREFIXES:
        channel = "#" + channel
    l_user = user.lower()

    try:
        for w_event in bot.send_and_wait(
            None,
            stope=("userKicked", *KICK_ERRORS),
            f=bot.kick,
            fargs=(channel, user, reason),
            timeout=WAIT_TIMEOUT,
        ):
            if (
                w_event.type == "userKicked"
                and w_event.target == channel
                and w_event.kicked.lower() == l_user
            ):
                r_msg = "Successfully kicked (%s) from (%s)" % (user, channel)
                if reason:
                    r_msg += " with reason (%s)" % esc(reason)
                bot.say(r_msg + ".")
                break
            r_msg = "Failed to kick (%s) from (%s): %s" % (user, channel, w_event.type)
            if w_event.params[2]:
                r_msg += " - %s" % w_event.params[2]
            bot.say(r_msg)
            break
    except TimeoutException:
        bot.say("Failed to kick (%s) in (%d second) timeout." % (channel, WAIT_TIMEOUT))


def send_msg_and_wait(bot: BotLike, chan_or_user: str, msg: str) -> bool | None:
    """
    Helper method to send a PRIVMSG and check for errors/success
    """
    try:
        for w_event in bot.send_and_wait(
            None,
            stope=(PRIVMSG_ERRORS),
            f=bot.sendmsg,
            fargs=(chan_or_user, msg),
            timeout=PRIVMSG_WAIT_TIMEOUT,
        ):
            r_msg = "Message (%s) to (%s) failed: %s" % (
                esc(msg),
                chan_or_user,
                w_event.type,
            )
            if w_event.params[2]:
                r_msg += " - %s" % w_event.params[2]
            bot.say(r_msg)
            return False
    except TimeoutException:
        # Assume success
        return True
    return True


def admin_msg(event: Event, bot: BotLike) -> None:
    msg = event.argument

    if event.isPM():
        chan_or_user, msg = commandSplit(msg)
        if not chan_or_user or not msg:
            return bot.say(".%s #channel message" % event.command)
    elif not msg:
        return bot.say(".%s message" % event.command)
    else:
        chan_or_user = event.target

    if chan_or_user is None:
        return bot.say("No message target available.")
    if send_msg_and_wait(bot, chan_or_user, msg) and event.isPM():
        bot.say("Successfully sent message to (%s)." % chan_or_user)


def admin_action(event: Event, bot: BotLike) -> None:
    msg = event.argument

    if event.isPM():
        chan_or_user, msg = commandSplit(msg)
        if not chan_or_user or not msg:
            return bot.say(".%s #channel action" % event.command)
    elif not msg:
        return bot.say(".%s action" % event.command)
    else:
        chan_or_user = event.target

    if chan_or_user is None:
        return bot.say("No action target available.")
    if send_msg_and_wait(bot, chan_or_user, "\x01ACTION %s\x01" % msg) and event.isPM():
        bot.say("Successfully sent action to (%s)." % chan_or_user)


def admin_rage(event: Event, bot: BotLike) -> None:
    msg = event.argument

    if event.isPM():
        chan_or_user, msg = commandSplit(msg)
        if not chan_or_user:
            return bot.say(".%s #channel FURIOUS_MESSAGE" % event.command)
    else:
        chan_or_user = event.target

    if not msg:
        msg = "A" * randint(200, 400)

    if chan_or_user is None:
        return bot.say("No message target available.")
    if send_msg_and_wait(bot, chan_or_user, AAA(msg)) and event.isPM():
        bot.say("Successfully sent FURIOUS_MESSAGE to (%s)." % chan_or_user)


mappings = (
    Mapping(command="join", function=admin_join, admin=True),
    Mapping(command="part", function=admin_part, admin=True),
    Mapping(command="kick", function=admin_kick, admin=True),
    Mapping(command=("msg", "message", "say", "pm"), function=admin_msg, admin=True),
    Mapping(command=("action", "me"), function=admin_action, admin=True),
    Mapping(command=("rage", "fury"), function=admin_rage, admin=True),
)
