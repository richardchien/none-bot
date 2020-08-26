import asyncio
import warnings
from typing import Awaitable, Set, Iterable, Optional, Callable, Union, NamedTuple

from aiocqhttp import Event as CQEvent
from aiocqhttp.message import Message

from .log import logger
from . import NoneBot
from .command import call_command
from .session import BaseSession
from .typing import CommandName_T, CommandArgs_T


class NLProcessor:
    __slots__ = ('func', 'keywords', 'only_to_me', 'only_short_message',
                 'allow_empty_message', 'perm_checker_func')

    def __init__(self, *, func: Callable, keywords: Optional[Iterable[str]],
                 only_to_me: bool, only_short_message: bool,
                 allow_empty_message: bool,
                 perm_checker_func: Callable[[NoneBot, CQEvent], Awaitable[bool]]):
        self.func = func
        self.keywords = keywords
        self.only_to_me = only_to_me
        self.only_short_message = only_short_message
        self.allow_empty_message = allow_empty_message
        self.perm_checker_func = perm_checker_func  # returns True if can trigger

    async def test(self, session: 'NLPSession', 
                   msg_text_length: Optional[int] = None) -> bool:
        """
        Test whether the session matches this (self) NL processor.

        :param session: NLPSession object
        :param msg_text_length: this argument is `len(session.msg_text)`,
                                designated to be cached if this function
                                is invoked in a loop
        :return: the session context matches this processor
        """
        if msg_text_length is None:
            msg_text_length = len(session.msg_text)

        if not self.allow_empty_message and not session.msg:
            # don't allow empty msg, but it is one, so no
            return False

        if self.only_short_message and \
            msg_text_length > session.bot.config.SHORT_MESSAGE_MAX_LENGTH:
            return False

        if self.only_to_me and not session.event['to_me']:
            return False

        should_run = await self._check_perm(session)
        if should_run and self.keywords:
            for kw in self.keywords:
                if kw in session.msg_text:
                    break
            else:
                # no keyword matches
                should_run = False
        return should_run

    async def _check_perm(self, session: 'NLPSession') -> bool:
        """
        Check if the session has sufficient permission to
        call the command.

        :param session: NLPSession object
        :return: the event has the permission
        """
        return await self.perm_checker_func(session.bot, session.event)


class NLPManager:
    _nl_processors: Set[NLProcessor] = set()

    def __init__(self):
        self.nl_processors = NLPManager._nl_processors.copy()

    @classmethod
    def add_nl_processor(cls, processor: NLProcessor) -> None:
        """Register a natural language processor
        
        Args:
            processor (NLProcessor): Processor object
        """
        if processor in cls._nl_processors:
            warnings.warn(f"NLProcessor {processor} already exists")
            return
        cls._nl_processors.add(processor)

    @classmethod
    def remove_nl_processor(cls, processor: NLProcessor) -> bool:
        """Remove a natural language processor globally
        
        Args:
            processor (NLProcessor): Processor to remove
        
        Returns:
            bool: Success or not
        """
        if processor in cls._nl_processors:
            cls._nl_processors.remove(processor)
            return True
        return False

    @classmethod
    def switch_nlprocessor_global(cls,
                                  processor: NLProcessor,
                                  state: Optional[bool] = None
                                  ) -> Optional[bool]:
        """Remove or add a natural language processor globally
        
        Args:
            processor (NLProcessor): Processor object
        
        Returns:
            bool: True if removed, False if added
        """
        if processor in cls._nl_processors and not state:
            cls._nl_processors.remove(processor)
            return True
        elif processor not in cls._nl_processors and state is not False:
            cls._nl_processors.add(processor)
            return False

    def switch_nlprocessor(self,
                           processor: NLProcessor,
                           state: Optional[bool] = None) -> Optional[bool]:
        """Remove or add a natural language processor
        
        Args:
            processor (NLProcessor): Processor to remove
        
        Returns:
            bool: True if removed, False if added
        """
        if processor in self.nl_processors and not state:
            self.nl_processors.remove(processor)
            return True
        elif processor not in self.nl_processors and state is not False:
            self.nl_processors.add(processor)
            return False


class NLPSession(BaseSession):
    __slots__ = ('msg', 'msg_text', 'msg_images')

    def __init__(self, bot: NoneBot, event: CQEvent, msg: str):
        super().__init__(bot, event)
        self.msg = msg
        tmp_msg = Message(msg)
        self.msg_text = tmp_msg.extract_plain_text()
        self.msg_images = [
            s.data['url']
            for s in tmp_msg
            if s.type == 'image' and 'url' in s.data
        ]


class NLPResult(NamedTuple):
    """
    Deprecated.
    Use class IntentCommand instead.
    """
    confidence: float
    cmd_name: Union[str, CommandName_T]
    cmd_args: Optional[CommandArgs_T] = None

    def to_intent_command(self):
        return IntentCommand(confidence=self.confidence,
                             name=self.cmd_name,
                             args=self.cmd_args)


class IntentCommand(NamedTuple):
    """
    To represent a command that we think the user may be intended to call.
    """
    confidence: float
    name: Union[str, CommandName_T]
    args: Optional[CommandArgs_T] = None
    current_arg: str = ''


async def handle_natural_language(bot: NoneBot, event: CQEvent,
                                  manager: NLPManager) -> bool:
    """
    Handle a message as natural language.

    This function is typically called by "handle_message".

    :param bot: NoneBot instance
    :param event: message event
    :param manager: natural language processor manager
    :return: the message is handled as natural language
    """
    session = NLPSession(bot, event, str(event.message))

    # use msg_text here because CQ code "share" may be very long,
    # at the same time some plugins may want to handle it
    msg_text_length = len(session.msg_text)

    futures = []
    for p in manager.nl_processors:
        should_run = await p.test(session, msg_text_length=msg_text_length)
        if should_run:
            futures.append(asyncio.ensure_future(p.func(session)))

    if futures:
        # wait for intent commands, and sort them by confidence
        intent_commands = []
        for fut in futures:
            try:
                res = await fut
                if isinstance(res, NLPResult):
                    intent_commands.append(res.to_intent_command())
                elif isinstance(res, IntentCommand):
                    intent_commands.append(res)
            except Exception as e:
                logger.error('An exception occurred while running '
                             'some natural language processor:')
                logger.exception(e)

        intent_commands.sort(key=lambda ic: ic.confidence, reverse=True)
        logger.debug(f'Intent commands: {intent_commands}')

        if intent_commands and intent_commands[0].confidence >= 60.0:
            # choose the intent command with highest confidence
            chosen_cmd = intent_commands[0]
            logger.debug(
                f'Intent command with highest confidence: {chosen_cmd}')
            return await call_command(bot,
                                      event,
                                      chosen_cmd.name,
                                      args=chosen_cmd.args,
                                      current_arg=chosen_cmd.current_arg,
                                      check_perm=False)  # type: ignore
        else:
            logger.debug('No intent command has enough confidence')
    return False
