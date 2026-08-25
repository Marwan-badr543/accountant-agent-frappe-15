# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""What a question looks like once it is sitting in somebody's chat history.

WHAT WAS WRONG

    A paused run answers with a JSON envelope, because the client needs the
    questions and their options as data. That envelope was being SAVED to the
    transcript and published as the assistant's message, so an accountant
    asking about a laptop sale got a chat bubble opening
    `{"type": "clarification", "questions": [{"id": ...` — with the question
    inside it twice. Their own reply was not stored at all, so the transcript
    showed a machine-readable question followed by nothing.

WHAT THESE TESTS PIN

    * The question is stored. The options are NOT: the choice has been made,
      the answer is the customer's own next message, and a list of the roads
      not taken between the two only makes the exchange harder to read.
    * The customer's answer comes out of the envelope the picker sends, and
      the question it echoes back does not.
    * The structured questions still ride along invisibly, so reloading the
      page on a run that is still paused reopens the answer picker.
    * The carrier never starts a paragraph of its own. It is `display: none`;
      a `<p>` wrapped around it is not, and an empty paragraph under every
      question is exactly the wasted space this work removed.
    * Arabic survives the round trip, which `atob` alone would not.

No site is needed: every function under test is pure.

RUN THEM FROM THE BENCH ROOT, not from the app directory:

    cd ~/frappe/my-bench && ./env/bin/python -m unittest \\
        accountant_agent.agent_api.tests.test_stored_questions

`frappe._()` builds the multi-question summary, and it opens `logs/frappe.log`
relative to the current directory.
"""

import json
import unittest
from base64 import b64decode

from accountant_agent.accountant_agent.page.agent_chat.agent_chat import (
	_answer_text,
	_collapsible_question,
	_readable_response,
)


ONE = [{
	"id": "party",
	"question": "من هو العميل في هذه العملية؟",
	"options": ["سجلها كبيع نقدي", "كانت بالآجل"],
	"allow_custom": True,
}]

TWO = ONE + [{"id": "amount", "question": "How much was it?", "options": []}]


def _payload(stored: str) -> list:
	"""The structured questions back out of the block, the way the browser
	reads them: find the attribute, un-base64 it, decode it as UTF-8."""
	packed = stored.split('data-questions="', 1)[1].split('"', 1)[0]
	return json.loads(b64decode(packed).decode("utf-8"))


class StoredQuestionTests(unittest.TestCase):
	def test_the_options_are_not_stored_in_the_transcript(self):
		stored = _collapsible_question(ONE[0]["question"], ONE)
		self.assertIn("من هو العميل في هذه العملية؟", stored)
		for option in ONE[0]["options"]:
			self.assertNotIn(option, stored)

	def test_one_question_is_not_wrapped_in_a_widget(self):
		"""A single sentence with nothing to fold is smaller than the block
		that would wrap it, and a caret that opens onto nothing is noise."""
		stored = _collapsible_question(ONE[0]["question"], ONE)
		self.assertNotIn("<details", stored)
		self.assertNotIn("<summary", stored)

	def test_the_carrier_does_not_take_a_paragraph_of_its_own(self):
		"""A blank line is a paragraph break to every Markdown renderer. The
		span is `display: none`; the `<p>` around it would not be."""
		stored = _collapsible_question(ONE[0]["question"], ONE)
		self.assertNotIn("\n\n<span", stored)
		self.assertEqual(len(stored.splitlines()), 1)
		self.assertTrue(stored.endswith("</span>"), stored)

	def test_a_reload_can_still_offer_the_answers(self):
		self.assertEqual(_payload(_collapsible_question("q", ONE)), ONE)

	def test_several_questions_fold_but_still_show_none_of_the_options(self):
		stored = _collapsible_question(TWO[0]["question"], TWO)
		self.assertIn("<details", stored)
		self.assertIn("(and 1 more)", stored)
		# The second question is what folds away. The first is the summary.
		self.assertIn("How much was it?", stored)
		for option in ONE[0]["options"]:
			self.assertNotIn(option, stored)
		self.assertEqual(_payload(stored), TWO)

	def test_a_question_with_no_wording_is_left_exactly_as_it_came(self):
		self.assertEqual(_collapsible_question("just this", []), "just this")
		self.assertEqual(
			_collapsible_question("just this", [{"id": "x", "question": ""}]),
			"just this",
		)


class AnswerTests(unittest.TestCase):
	def test_the_answer_is_stored_and_the_echoed_question_is_not(self):
		said = _answer_text(
			"Clarification Response:\n"
			"* **من هو العميل في هذه العملية؟**: سجلها كبيع نقدي"
		)
		self.assertEqual(said, "سجلها كبيع نقدي")

	def test_several_answers_are_stored_one_per_line(self):
		said = _answer_text(
			"Clarification Response:\n"
			"* **Who was it with?**: Delta Supplies\n"
			"* **How much?**: 500"
		)
		self.assertEqual(said, "Delta Supplies\n500")

	def test_nothing_to_show_stores_nothing(self):
		self.assertEqual(_answer_text("just a message"), "")
		self.assertEqual(_answer_text(""), "")


class EnvelopeTests(unittest.TestCase):
	def test_a_clarification_envelope_is_read_into_prose_and_questions(self):
		spoken, questions = _readable_response(json.dumps({
			"type": "clarification",
			"question": "من هو العميل في هذه العملية؟",
			"questions": ONE,
		}))
		self.assertEqual(spoken, "من هو العميل في هذه العملية؟")
		self.assertEqual(questions, ONE)

	def test_a_plan_envelope_is_left_byte_for_byte_alone(self):
		"""The approval gate finds a pending plan by matching the literal
		prefix `{"type": "plan"`. Prettifying it here would silently stop
		`Require Human Approval` working, from a function about wording."""
		plan = '{"type": "plan", "steps": []}'
		spoken, questions = _readable_response(plan)
		self.assertEqual(spoken, plan)
		self.assertEqual(questions, [])

	def test_an_ordinary_answer_is_not_touched(self):
		spoken, questions = _readable_response("I have recorded that for you.")
		self.assertEqual(spoken, "I have recorded that for you.")
		self.assertEqual(questions, [])


if __name__ == "__main__":
	unittest.main()
