# Ticket: default page limit disagrees with the docs

The rate card allows 25 items per page, but DEFAULT_LIMIT is 50. Correct the constant; both helpers derive from it.