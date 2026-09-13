# Ticket: Celsius conversion drops the offset

`celsius_to_fahrenheit` scales but never adds 32, so 0C maps to 0F. Add the missing offset; `freezing_point()` is correct and must keep working.