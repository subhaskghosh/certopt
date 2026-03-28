SELECT
  p.firstname,
  p.lastname,
  e.city,
  e.state
FROM PERSON AS P
LEFT JOIN ADDRESS AS E
  ON e.personid = p.personid