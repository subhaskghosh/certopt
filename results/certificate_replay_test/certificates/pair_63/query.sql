SELECT
  p.firstname AS FIRSTNAME,
  p.lastname,
  a.city,
  a.state
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON a.personid = p.personid