SELECT
  t.firstname,
  t.lastname,
  p.city,
  p.state
FROM PERSON AS T
LEFT JOIN ADDRESS AS P
  ON p.personid = t.personid