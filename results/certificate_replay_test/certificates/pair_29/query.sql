SELECT
  p.firstname,
  p.lastname,
  a.city,
  a.state
FROM ADDRESS AS A
RIGHT JOIN PERSON AS P
  ON a.personid = p.personid