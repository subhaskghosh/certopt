SELECT
  c.firstname,
  c.lastname,
  a.city,
  a.state
FROM PERSON AS C
LEFT JOIN ADDRESS AS A
  ON a.personid = c.personid