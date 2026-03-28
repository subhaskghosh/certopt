SELECT
  firstname,
  lastname,
  city,
  state
FROM ADDRESS AS A
RIGHT JOIN PERSON AS B
  ON a.personid = b.personid