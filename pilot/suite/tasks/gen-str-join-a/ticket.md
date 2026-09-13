# Ticket: CSV lines use the wrong separator

`csv_line(["a", "b"])` yields `a;b`. Use commas; `csv_header()` is correct.