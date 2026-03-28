SELECT
  a.firstname,
  a.lastname,
  b.city,
  b.state
FROM PERSON AS A
LEFT JOIN ADDRESS AS B
  ON personid = b.personid