SELECT
  p.firstname,
  p.lastname,
  city,
  state
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON a.personid = p.personid