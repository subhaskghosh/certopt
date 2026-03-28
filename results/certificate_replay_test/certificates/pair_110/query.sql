SELECT
  e.firstname,
  e.lastname,
  a.city,
  a.state
FROM PERSON AS E
LEFT JOIN ADDRESS AS A
  ON a.personid = e.personid