SELECT
  p.firstname,
  p.lastname,
  a.city,
  a.state
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON personid = a.personid